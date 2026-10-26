"""Discord command registration for /stock_ai.

Keeps Discord-facing wiring separate from cache/analyzer implementation.
Supports both single-stock full diagnosis/QA and multi-stock comma-separated comparison.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

import discord
from discord import app_commands

from ai_service import ask_gemini
from fundamental_analyzer import FundamentalAnalyzer
from fundamental_cache import FundamentalCacheError, FundamentalCacheManager
from fundamental_config import PROMPT_TEMPLATES

logger = logging.getLogger(__name__)

_STOCK_ID_PATTERN = re.compile(r"^\d{4,6}$")
_MAX_MULTI_STOCKS = 4


def _parse_symbols(raw_symbol: str) -> list[str]:
    """解析單股或逗號分隔多股（如 '2357, 2330'），去重並保留順序。"""
    tokens = [t.strip() for t in re.split(r"[,，\s]+", raw_symbol.strip()) if t.strip()]
    unique_tokens: list[str] = []
    for t in tokens:
        if t not in unique_tokens:
            unique_tokens.append(t)
    return unique_tokens


def _split_discord_chunks(text: str, max_len: int = 1900) -> list[str]:
    """將過長文字安全切分成符合 Discord 單則限制 (<= 2000 字) 的文字塊，優先在換行處切割。"""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_len and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


class StockWarRoomView(discord.ui.View):
    def __init__(
        self,
        *,
        war_room_url: str | None,
        chart_url: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        if war_room_url:
            self.add_item(
                discord.ui.Button(
                    label="📊 全方位戰情室（含財報+K線）",
                    style=discord.ButtonStyle.link,
                    url=war_room_url,
                )
            )
        elif chart_url:
            self.add_item(
                discord.ui.Button(
                    label="📈 開啟技術面互動 K 線圖",
                    style=discord.ButtonStyle.link,
                    url=chart_url,
                )
            )


def register_stock_ai_command(tree: app_commands.CommandTree) -> None:
    cache = FundamentalCacheManager()
    analyzer = FundamentalAnalyzer(cache)

    @tree.command(name="stock_ai", description="台股基本面三大財報、股利與籌碼智慧快取分析")
    @app_commands.describe(
        symbol="台股代號（必填，單股如 2357，或多股比較如 2357,2330）",
        prompt="想詢問 AI 的特定財報問題（選填；若不填則產出完整體質診斷）",
        force_refresh="是否強制重抓最新資料並重跑 AI 分析（預設 False）",
    )
    async def stock_ai(
        interaction: discord.Interaction,
        symbol: str,
        prompt: str | None = None,
        force_refresh: bool = False,
    ) -> None:
        stock_ids = _parse_symbols(symbol)
        if not stock_ids or any(not _STOCK_ID_PATTERN.fullmatch(sid) for sid in stock_ids):
            await interaction.response.send_message(
                "⚠️ `symbol` 必須是 4~6 位數字台股代號（多股請用逗號分隔，例如 `2357,2330`）。",
                ephemeral=True,
            )
            return

        if len(stock_ids) > _MAX_MULTI_STOCKS:
            await interaction.response.send_message(
                f"⚠️ 單次比較最多支援 {_MAX_MULTI_STOCKS} 檔股票。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        # 1. 依序同步所有指定股票的 Google Sheets 快取
        for sid in stock_ids:
            try:
                await asyncio.to_thread(cache.check_and_sync_stock, sid, force_refresh)
            except FundamentalCacheError as error:
                await interaction.followup.send(f"⚠️ [{sid}] {error}")
                return
            except Exception:
                logger.exception("Failed to sync fundamental cache for %s", sid)
                await interaction.followup.send(f"❌ [{sid}] 財報快取同步失敗，請稍後再試。")
                return

        # 取得股票名稱輔助函式
        def _get_display_title(sid: str) -> str:
            sname, _ = cache._get_stock_basic_info(sid)  # noqa: SLF001
            return f"{sid} {sname}".strip() if sname else sid

        # 2. 多股模式（純文字發送，方便長按一鍵複製）
        if len(stock_ids) > 1:
            try:
                contexts = [
                    await asyncio.to_thread(analyzer.build_markdown_context, sid)
                    for sid in stock_ids
                ]
                user_q = (
                    prompt.strip()
                    if (prompt and prompt.strip())
                    else "請針對以上多檔股票的獲利能力（三率與EPS）、CCC營運週轉效率、自由現金流含金量、股利政策與近期法人籌碼進行全方位橫向比較，並給出優劣排序結論。"
                )
                combined_prompt = (
                    "\n\n".join(contexts)
                    + "\n\n==================================================\n"
                    + "【跨股比較與使用者詢問問題】\n"
                    + user_q
                    + "\n\n"
                    + PROMPT_TEMPLATES["targeted_qa"]
                )
                answer = await ask_gemini(combined_prompt)
            except Exception:
                logger.exception("Multi-stock QA failed for %s", stock_ids)
                await interaction.followup.send("❌ 多股 AI 比較分析失敗，請稍後再試。")
                return

            titles_str = " vs ".join(_get_display_title(sid) for sid in stock_ids)
            header = f"⚖️ **{titles_str}｜財報跨股比較與解答**\n\n"
            full_text = header + answer
            for chunk in _split_discord_chunks(full_text):
                await interaction.followup.send(chunk)
            return

        # 3. 單股模式
        stock_id = stock_ids[0]
        stock_title = _get_display_title(stock_id)

        if prompt and prompt.strip():
            try:
                answer = await analyzer.run_targeted_qa(stock_id, prompt)
            except Exception:
                logger.exception("Targeted QA failed for %s", stock_id)
                await interaction.followup.send("❌ AI 分析失敗，請稍後再試。")
                return

            header = f"📌 **{stock_title}｜財報專題解答**\n\n"
            full_text = header + answer
            for chunk in _split_discord_chunks(full_text):
                await interaction.followup.send(chunk)
            return

        # 依需求：每次 /stock_ai 都重新呼叫 Gemini 產生最新診斷文字
        try:
            report = await analyzer.run_full_diagnosis(stock_id, force_ai=True)
        except Exception:
            logger.exception("Full diagnosis failed for %s", stock_id)
            await interaction.followup.send("❌ AI 診斷失敗，請稍後再試。")
            return

        chart_url = None
        try:
            owner_repo = (os.getenv("GITHUB_REPO", "").strip() or "").split("/")
            if len(owner_repo) == 2:
                owner, repo = owner_repo
                base = f"https://{owner}.github.io/{repo}/charts/{stock_id}.html"
                if repo.lower() == f"{owner}.github.io".lower():
                    base = f"https://{owner}.github.io/charts/{stock_id}.html"
                chart_url = base
        except Exception:
            chart_url = None

        header = f"📊 **{stock_title}｜基本面 AI 診斷**\n\n"
        full_text = header + report.ai_text
        if report.cache_period:
            full_text += f"\n\n*(資料水位：{report.cache_period} | cached={report.cached})*"

        has_links = bool(report.war_room_url or chart_url)
        view = (
            StockWarRoomView(
                war_room_url=report.war_room_url,
                chart_url=chart_url,
            )
            if has_links
            else None
        )

        chunks = _split_discord_chunks(full_text)
        for i, chunk in enumerate(chunks):
            # 互動按鈕掛載在最後一則訊息底下
            if i == len(chunks) - 1 and view is not None:
                await interaction.followup.send(chunk, view=view)
            else:
                await interaction.followup.send(chunk)