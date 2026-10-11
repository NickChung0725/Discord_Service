"""Standalone Discord entry point for the stock chart slash command."""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from ai_weather import register_ai_command, register_weather_command
from chart_generator import (
    InvalidStockIDError,
    StockNotFoundError,
    generate_stock_html_with_summary,
)
from github_uploader import upload_to_github

logger = logging.getLogger(__name__)
load_dotenv()


def register_stock_command(
    tree: app_commands.CommandTree,
) -> None:
    """Register /stock on a Discord client's application command tree."""

    @tree.command(name="stock", description="產生台股近半年技術分析圖表")
    @app_commands.describe(stock_id="台灣股票代號，例如 2330")
    async def stock(interaction: discord.Interaction, stock_id: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            file_path, closing_price, trading_date = await asyncio.to_thread(
                generate_stock_html_with_summary, stock_id
            )
            pages_url = await asyncio.to_thread(upload_to_github, file_path, stock_id.strip())
        except (InvalidStockIDError, StockNotFoundError) as error:
            await interaction.followup.send(f"⚠️ {error}")
            return
        except Exception:
            logger.exception("Failed to generate or publish stock chart for %r", stock_id)
            await interaction.followup.send(
                "❌ 圖表處理失敗，請稍後再試；管理員可查看機器人日誌。"
            )
            return

        embed = discord.Embed(
            title=f"📈 {stock_id.strip()} 技術分析",
            description=f"最新收盤日：**{trading_date}**",
            color=discord.Color.blurple(),
            url=pages_url,
        )
        embed.add_field(name="最新收盤價", value=f"NT$ {closing_price:,.2f}", inline=True)
        embed.add_field(name="互動圖表", value=f"[開啟 GitHub Pages]({pages_url})", inline=True)
        embed.set_footer(text="資料來源：FinMind；指標僅供參考，非投資建議")
        await interaction.followup.send(embed=embed)


class StockBot(commands.Bot):
    async def setup_hook(self) -> None:
        await self.tree.sync()
        logger.info("Global application commands synced.")


intents = discord.Intents.default()
bot = StockBot(command_prefix="!", intents=intents)
register_stock_command(bot.tree)
register_ai_command(bot.tree)
register_weather_command(bot.tree)


if __name__ == "__main__":
    discord_token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
    if not discord_token:
        raise RuntimeError("DISCORD_TOKEN (or legacy BOT_TOKEN) is missing.")
    logging.basicConfig(level=logging.INFO)
    bot.run(discord_token)
