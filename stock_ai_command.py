"""Discord command registration for /stock_ai.

Keeps Discord-facing wiring separate from cache/analyzer implementation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import discord
from discord import app_commands

from fundamental_analyzer import FundamentalAnalyzer
from fundamental_cache import FundamentalCacheManager, FundamentalCacheError

logger = logging.getLogger(__name__)

_STOCK_ID_PATTERN = re.compile(r"^\d{4,6}$")


class StockWarRoomView(discord.ui.View):
    def __init__(
        self,
        *,
        stock_id: str,
        war_room_url: str | None,
        chart_url: str | None,
        analyzer: FundamentalAnalyzer,
    ) -> None:
        super().__init__(timeout=300)
        self.stock_id = stock_id
        self.war_room_url = war_room_url
        self.chart_url = chart_url
        self.analyzer = analyzer

        if war_room_url:
            self.add_item(
                discord.ui.Button(
                    label="📊 開啟 AI 財報圖文戰情室",
                    style=discord.ButtonStyle.link,
                    url=war_room_url,
                )
            )
        if chart_url:
            self.add_item(
                discord.ui.Button(
                    label="📈 開啟技術面互動 K 線圖",
                    style=discord.ButtonStyle.link,
                    url=chart_url,
                )
            )

    @discord.ui.button(label="🔄 重新呼叫 AI 分析", style=discord.ButtonStyle.primary)
    async def rerun_ai(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(thinking=True)
        try:
            report = await self.analyzer.run_full_diagnosis(self.stock_id, force_ai=True)
        except Exception:
            logger.exception("Failed to rerun AI analysis for %s", self.stock_id)
            await interaction.followup.send("❌ AI 重新分析失敗，請稍後再試。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📊 {self.stock_id} 基本面 AI 診斷（已重新分析）",
            description=report.ai_text[:3900],
            color=discord.Color.orange(),
            url=report.war_room_url,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


def register_stock_ai_command(tree: app_commands.CommandTree) -> None:
    cache = FundamentalCacheManager()
    analyzer = FundamentalAnalyzer(cache)

    @tree.command(name="stock_ai", description="台股基本面三大財報與月營收智慧快取分析")
    @app_commands.describe(
        symbol="台股代號（必填，單股如 2330）",
        prompt="想詢問 AI 的特定財報問題（選填；若不填則產出完整體質診斷）",
        force_refresh="是否強制重抓最新資料並重跑 AI 分析（預設 False）",
    )
    async def stock_ai(
        interaction: discord.Interaction,
        symbol: str,
        prompt: str | None = None,
        force_refresh: bool = False,
    ) -> None:
        stock_id = symbol.strip()
        if not _STOCK_ID_PATTERN.fullmatch(stock_id):
            await interaction.response.send_message(
                "⚠️ symbol 必須是 4~6 位數字（v1 先不支援逗號多股）。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            _ = await asyncio.to_thread(
                cache.check_and_sync_stock, stock_id, force_refresh
            )
        except FundamentalCacheError as error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        except Exception:
            logger.exception("Failed to sync fundamental cache for %s", stock_id)
            await interaction.followup.send("❌ 財報快取同步失敗，請稍後再試。")
            return

        # Route based on prompt.
        if prompt and prompt.strip():
            try:
                answer = await analyzer.run_targeted_qa(stock_id, prompt)
            except Exception:
                logger.exception("Targeted QA failed for %s", stock_id)
                await interaction.followup.send("❌ AI 分析失敗，請稍後再試。")
                return

            embed = discord.Embed(
                title=f"📌 {stock_id} 財報專題解答",
                description=answer[:3900],
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            report = await analyzer.run_full_diagnosis(stock_id, force_ai=force_refresh)
        except Exception:
            logger.exception("Full diagnosis failed for %s", stock_id)
            await interaction.followup.send("❌ AI 診斷失敗，請稍後再試。")
            return

        # Tech chart url follows existing /stock uploader convention.
        chart_url = None
        try:
            # The existing uploader returns pages url; we can derive charts/{id}.html with env.
            owner_repo = (os.getenv("GITHUB_REPO", "").strip() or "").split("/")
            if len(owner_repo) == 2:
                owner, repo = owner_repo
                base = f"https://{owner}.github.io/{repo}/charts/{stock_id}.html"
                if repo.lower() == f"{owner}.github.io".lower():
                    base = f"https://{owner}.github.io/charts/{stock_id}.html"
                chart_url = base
        except Exception:
            chart_url = None

        embed = discord.Embed(
            title=f"📊 {stock_id} 基本面 AI 診斷",
            description=report.ai_text[:3900],
            color=discord.Color.green() if report.cached else discord.Color.gold(),
            url=report.war_room_url,
        )
        if report.cache_period:
            embed.set_footer(text=f"資料水位：{report.cache_period} | cached={report.cached}")

        view = StockWarRoomView(
            stock_id=stock_id,
            war_room_url=report.war_room_url,
            chart_url=chart_url,
            analyzer=analyzer,
        )
        await interaction.followup.send(embed=embed, view=view)

