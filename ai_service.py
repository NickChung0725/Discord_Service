"""Dual-provider Gemini AI service:
- Google AI Studio (free tier via GEMINI_API_KEY / GEMINI_MODEL): used for general /ai, /stock_ai,
  and per-stock fundamental diagnosis.
- Google Cloud Vertex AI (paid tier via GOOGLE_CLOUD_PROJECT_ID / GOOGLE_CLOUD_LOCATION / VERTEX_API_KEY
  / VERTEX_MODEL): used for the 22:00 / 22:30 cross-stock portfolio selection decision.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import discord
from discord import app_commands
from google import genai

logger = logging.getLogger(__name__)


class MissingGeminiApiKeyError(RuntimeError):
    """Raised when Google AI Studio is called without GEMINI_API_KEY."""


class MissingVertexConfigError(RuntimeError):
    """Raised when Vertex AI is called without required GCP project/credential settings."""


DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_VERTEX_MODEL = "gemini-2.5-flash-lite"
DEFAULT_VERTEX_LOCATION = "us-central1"

_RETRYABLE_ERROR_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "resource_exhausted",
    "unavailable",
    "overloaded",
    "quota",
    "rate limit",
    "deadline_exceeded",
    "deadline expired",
    "timed out",
    "timeout",
    "internalservererror",
)
_TRANSIENT_RETRY_DELAYS_SECONDS = (3.0, 6.0, 12.0)


def _is_retryable_gemini_error(error: BaseException) -> bool:
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in _RETRYABLE_ERROR_MARKERS)


# --------------------------------------------------------------------------- #
# 1. Google AI Studio (Free Tier)
# --------------------------------------------------------------------------- #
def get_gemini_model() -> str:
    """Return configured Google AI Studio model from GEMINI_MODEL or default."""
    return (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()


def _generate_ai_response(prompt: str, model: str | None = None) -> str:
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise MissingGeminiApiKeyError("GEMINI_API_KEY is not configured.")

    client = genai.Client(api_key=api_key)
    resolved_model = (model or get_gemini_model()).strip()

    response = client.interactions.create(
        model=resolved_model,
        input=prompt,
    )
    answer = response.output_text
    if not answer:
        raise RuntimeError("Google AI Studio returned an empty response.")
    return answer


def _call_google_ai(prompt: str, model: str | None) -> str:
    if model is None:
        return _generate_ai_response(prompt)
    return _generate_ai_response(prompt, model)


def _generate_with_fallback(
    prompt: str, model: str | None, fallback_model: str | None
) -> str:
    """Call `model` on Google AI Studio; on transient/quota/timeout errors retry or degrade to `fallback_model`."""
    try:
        return _call_google_ai(prompt, model)
    except MissingGeminiApiKeyError:
        raise
    except Exception as error:  # noqa: BLE001
        if not _is_retryable_gemini_error(error):
            raise
        target_model = fallback_model if fallback_model is not None else model
        if fallback_model:
            logger.warning(
                "Gemini model %s unavailable (%s); falling back to %s.",
                model,
                error,
                fallback_model,
            )
        else:
            logger.warning(
                "Gemini model %s transient error (%s); retrying with backoff...",
                model or get_gemini_model(),
                error,
            )

    if fallback_model is not None:
        # First attempt on fallback_model immediately, then backoff on further transient errors
        retry_schedule: tuple[float | None, ...] = (*_TRANSIENT_RETRY_DELAYS_SECONDS, None)
    else:
        # Same model: sleep before each retry
        for delay in _TRANSIENT_RETRY_DELAYS_SECONDS:
            time.sleep(delay)
            try:
                return _call_google_ai(prompt, target_model)
            except MissingGeminiApiKeyError:
                raise
            except Exception as error:  # noqa: BLE001
                if not _is_retryable_gemini_error(error):
                    raise
        return _call_google_ai(prompt, target_model)

    for delay in retry_schedule:
        try:
            return _call_google_ai(prompt, target_model)
        except MissingGeminiApiKeyError:
            raise
        except Exception as error:  # noqa: BLE001
            if delay is None or not _is_retryable_gemini_error(error):
                raise
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


async def ask_gemini(
    prompt: str,
    *,
    model: str | None = None,
    fallback_model: str | None = None,
) -> str:
    """Ask Google AI Studio (free tier). `model` overrides GEMINI_MODEL; `fallback_model` enables quota fallback."""
    return await asyncio.to_thread(_generate_with_fallback, prompt, model, fallback_model)


# --------------------------------------------------------------------------- #
# 2. Google Cloud Vertex AI (Paid Tier - for 22:00 / 22:30 Portfolio Decision)
# --------------------------------------------------------------------------- #
def get_vertex_model() -> str:
    """Return configured Vertex AI model from .env VERTEX_MODEL (primary authority)."""
    return (os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_MODEL).strip()


def _generate_vertex_ai_response(prompt: str, model: str | None = None) -> str:
    project_id = (os.getenv("GOOGLE_CLOUD_PROJECT_ID") or "").strip()
    location = (os.getenv("GOOGLE_CLOUD_LOCATION") or DEFAULT_VERTEX_LOCATION).strip()
    vertex_api_key = (os.getenv("VERTEX_API_KEY") or "").strip() or None

    if not project_id:
        raise MissingVertexConfigError("GOOGLE_CLOUD_PROJECT_ID is not configured.")

    resolved_model = (model or get_vertex_model()).strip()
    client_kwargs: dict[str, Any] = {
        "vertexai": True,
        "project": project_id,
        "location": location,
    }
    if vertex_api_key:
        client_kwargs["api_key"] = vertex_api_key

    client = genai.Client(**client_kwargs)
    response = client.models.generate_content(
        model=resolved_model,
        contents=prompt,
    )
    answer = response.text
    if not answer:
        raise RuntimeError("Vertex AI returned an empty response.")
    return answer


def _generate_vertex_with_retry(prompt: str, model: str | None = None) -> str:
    """Call Vertex AI with transient-error retries ONLY within Vertex AI.
    Never degrades to free-tier Google AI Studio so portfolio decision quality stays guaranteed.
    """
    resolved_model = (model or get_vertex_model()).strip()
    for delay in (*_TRANSIENT_RETRY_DELAYS_SECONDS, None):
        try:
            return _generate_vertex_ai_response(prompt, resolved_model)
        except MissingVertexConfigError:
            raise
        except Exception as error:  # noqa: BLE001
            if delay is None or not _is_retryable_gemini_error(error):
                raise
            logger.warning(
                "Vertex AI model %s transient error (%s); retrying in %.1fs...",
                resolved_model,
                error,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")  # pragma: no cover


async def ask_vertex_ai(
    prompt: str,
    *,
    model: str | None = None,
) -> str:
    """Ask Google Cloud Vertex AI (paid tier) for high-reliability decision tasks."""
    return await asyncio.to_thread(_generate_vertex_with_retry, prompt, model)


# --------------------------------------------------------------------------- #
# 3. Discord /ai Slash Command
# --------------------------------------------------------------------------- #
def _discord_messages(text: str) -> list[str]:
    return [text[index : index + 2000] for index in range(0, len(text), 2000)]


def register_ai_command(tree: app_commands.CommandTree) -> None:
    """Register /ai, which answers one prompt using Google AI Studio."""

    @tree.command(name="ai", description="使用 Google AI Studio 的 Gemini 回答問題")
    @app_commands.describe(prompt="想詢問 AI 的問題")
    async def ai(interaction: discord.Interaction, prompt: str) -> None:
        if not prompt.strip():
            await interaction.response.send_message("請輸入想詢問 AI 的問題。", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            answer = await ask_gemini(prompt.strip())
        except MissingGeminiApiKeyError:
            await interaction.followup.send(
                "❌ 尚未設定 `GEMINI_API_KEY`，請管理員在環境變數或 `.env` 中設定 Google AI Studio API Key。"
            )
            return
        except RuntimeError:
            logger.exception("Google AI Studio returned no answer.")
            await interaction.followup.send("❌ AI 暫時無法提供回覆，請稍後再試。")
            return
        except Exception:
            logger.exception("Google AI Studio request failed.")
            await interaction.followup.send("❌ AI 服務呼叫失敗，請稍後再試。")
            return

        for message in _discord_messages(answer):
            await interaction.followup.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
