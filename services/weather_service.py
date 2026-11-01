"""Taipei weather forecast features (Open-Meteo) for the Discord bot."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
import requests
from discord import app_commands

logger = logging.getLogger(__name__)

TAIPEI_TIMEZONE = timezone(timedelta(hours=8))
TAIPEI_LATITUDE = 25.033
TAIPEI_LONGITUDE = 121.5654
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

_WEATHER_DESCRIPTIONS = {
    0: "晴朗",
    1: "大致晴朗",
    2: "多雲時晴",
    3: "陰天",
    45: "有霧",
    48: "有霧凇",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "較強毛毛雨",
    56: "凍毛毛雨",
    57: "較強凍毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "凍雨",
    67: "較強凍雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "零星陣雨",
    81: "陣雨",
    82: "強陣雨",
    85: "陣雪",
    86: "強陣雪",
    95: "雷雨",
    96: "雷雨伴隨冰雹",
    99: "強雷雨伴隨冰雹",
}


def _fetch_weather_data() -> dict[str, Any]:
    response = requests.get(
        WEATHER_URL,
        params={
            "latitude": TAIPEI_LATITUDE,
            "longitude": TAIPEI_LONGITUDE,
            "timezone": "Asia/Taipei",
            "forecast_days": 1,
            "current": (
                "temperature_2m,relative_humidity_2m,apparent_temperature,"
                "precipitation,weather_code,wind_speed_10m"
            ),
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,"
                "precipitation_probability_max,sunrise,sunset"
            ),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _weather_description(code: int) -> str:
    return _WEATHER_DESCRIPTIONS.get(code, f"天氣狀況不明（代碼 {code}）")


def format_weather_report(
    data: dict[str, Any],
    now: datetime | None = None,
) -> str:
    """Format an Open-Meteo response as a concise Traditional Chinese report."""
    try:
        current = data["current"]
        daily = data["daily"]
        date = daily["time"][0]
        current_code = int(current["weather_code"])
        forecast_code = int(daily["weather_code"][0])
        temperature = float(current["temperature_2m"])
        feels_like = float(current["apparent_temperature"])
        humidity = int(current["relative_humidity_2m"])
        wind_speed = float(current["wind_speed_10m"])
        precipitation = float(current["precipitation"])
        high = float(daily["temperature_2m_max"][0])
        low = float(daily["temperature_2m_min"][0])
        rain_chance = int(daily["precipitation_probability_max"][0])
        sunrise = datetime.fromisoformat(daily["sunrise"][0]).strftime("%H:%M")
        sunset = datetime.fromisoformat(daily["sunset"][0]).strftime("%H:%M")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError("Weather provider returned incomplete or invalid data.") from error

    report_date = date
    if now is not None:
        report_date = now.astimezone(TAIPEI_TIMEZONE).strftime("%Y-%m-%d")

    return (
        f"🌤️ **台北今日天氣**（{report_date}）\n"
        f"目前：{_weather_description(current_code)}，"
        f"{temperature:.1f}°C（體感 {feels_like:.1f}°C）\n"
        f"今日預報：{_weather_description(forecast_code)}，"
        f"最高 {high:.1f}°C／最低 {low:.1f}°C\n"
        f"降雨機率：{rain_chance}%｜目前降雨量：{precipitation:.1f} mm\n"
        f"相對濕度：{humidity}%｜風速：{wind_speed:.1f} km/h\n"
        f"日出 {sunrise}｜日落 {sunset}\n"
        "資料來源：Open-Meteo"
    )


async def get_weather_report() -> str:
    data = await asyncio.to_thread(_fetch_weather_data)
    return format_weather_report(data)


def register_weather_command(tree: app_commands.CommandTree) -> None:
    """Register /weather for an on-demand Taipei forecast."""

    @tree.command(name="weather", description="查詢台北今日天氣")
    async def weather(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            report = await get_weather_report()
        except Exception:
            logger.exception("Failed to fetch Taipei weather.")
            await interaction.followup.send("❌ 天氣資料取得失敗，請稍後再試。")
            return

        await interaction.followup.send(report)
