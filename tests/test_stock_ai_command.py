"""Tests for /stock_ai command registration and dynamic tier display names."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import discord
from discord.ext import commands

from services import stock_ai_command


class TestStockAICommandTierDisplay(unittest.TestCase):
    def test_tier_choices_and_description_follow_env_models(self) -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        with (
            patch.dict(
                os.environ,
                {
                    "STAGE1_MODEL": "gemini-4-hydrogen",
                    "VERTEX_STAGE1_MODEL": "gemini-4-argon",
                },
                clear=False,
            ),
            patch.object(stock_ai_command, "FundamentalCacheManager"),
            patch.object(stock_ai_command, "FundamentalAnalyzer"),
        ):
            stock_ai_command.register_stock_ai_command(bot.tree)

        cmd = bot.tree.get_command("stock_ai")
        self.assertIsNotNone(cmd)
        tier_param = next(p for p in cmd.parameters if p.name == "tier")

        self.assertEqual(tier_param.default, "standard")
        self.assertEqual(
            [(c.name, c.value) for c in tier_param.choices],
            [
                ("標準 (gemini-4-hydrogen)", "standard"),
                ("大師 (gemini-4-argon)", "master"),
            ],
        )
        self.assertIn("標準 (gemini-4-hydrogen)", tier_param.description)
        self.assertIn("大師 (gemini-4-argon)", tier_param.description)
        self.assertLessEqual(len(tier_param.description), 100)


if __name__ == "__main__":
    unittest.main()
