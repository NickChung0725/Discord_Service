"""Dual-provider Gemini AI service:
- Google AI Studio (free tier via GEMINI_API_KEY / GEMINI_MODEL): used for general /ai, /stock_ai,
  and per-stock fundamental diagnosis.
- Google Cloud Vertex AI (paid tier via GOOGLE_CLOUD_PROJECT_ID / GOOGLE_CLOUD_LOCATION / VERTEX_API_KEY
  / VERTEX_MODEL): used for the 22:00 / 22:30 cross-stock portfolio selection decision.

SASD v3.5.1:
- Migrate Google AI Studio calls from `client.interactions.create` to `client.models.generate_content`.
- Add per-day consolidated AI call log: finmind_reports/logs/YYYY-MM-DD_ai_calls.log.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import discord
from discord import app_commands
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


class MissingGeminiApiKeyError(RuntimeError):
    """Raised when Google AI Studio is called without GEMINI_API_KEY."""


class MissingVertexConfigError(RuntimeError):
    """Raised when Vertex AI is called without required GCP project/credential settings."""


@dataclass(frozen=True, slots=True)
class AIResponse:
    provider: str
    requested_model: str
    model_version: str
    text: str
    prompt_tokens: int | None
    output_tokens: int | None


DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_VERTEX_MODEL = "gemini-2.5-flash-lite"
DEFAULT_VERTEX_LOCATION = "us-central1"

GOOGLE_AI_THINKING_BUDGET = 128

# SASD v4.0 Stage 1 單股基本面診斷：改用付費版 gemini-3.8-flash（可由 .env STAGE1_MODEL 覆寫）。
# 停用 flash-lite 降級（避免財報推論 JSON 欄位違規與期別錯亂）；如需降級可設定 STAGE1_FALLBACK_MODEL。
DEFAULT_STAGE1_MODEL = "gemini-3.8-flash"
DIAG_PRIMARY_MODEL = DEFAULT_STAGE1_MODEL
DIAG_PRIMARY_RETRY_DELAYS_SECONDS = (15.0, 30.0, 45.0)
DIAG_FALLBACK_MODEL: str | None = None


def get_stage1_model() -> str:
    """Stage 1 model from .env STAGE1_MODEL (read lazily so load_dotenv ordering never matters)."""
    return (os.getenv("STAGE1_MODEL") or DEFAULT_STAGE1_MODEL).strip()


def get_stage1_fallback_model() -> str | None:
    return (os.getenv("STAGE1_FALLBACK_MODEL") or "").strip() or None
DIAG_FALLBACK_RETRY_DELAYS_SECONDS = (5.0, 10.0, 20.0)

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


def _is_diag_retryable_error(error: BaseException) -> bool:
    """22:00 單股診斷專用：只針對 free-tier 常見 429/503 類問題做長等待重試。"""
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in ("429", "503", "resource_exhausted", "quota", "unavailable"))


# --------------------------------------------------------------------------- #
# Daily consolidated AI call log
# --------------------------------------------------------------------------- #
_LOG_LOCK = threading.Lock()

try:  # pragma: no cover
    import fcntl  # type: ignore
except Exception:  # pragma: no cover
    fcntl = None  # type: ignore


def _finmind_reports_dir() -> Path:
    env_reports_dir = os.getenv("FINMIND_REPORTS_DIR", "").strip()
    if env_reports_dir:
        return Path(env_reports_dir)
    return Path(__file__).resolve().parent / "finmind_reports"


def _ai_logs_dir() -> Path:
    return _finmind_reports_dir() / "logs"


TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


def _ai_log_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(TAIPEI_TIMEZONE)).astimezone(TAIPEI_TIMEZONE).strftime("%Y-%m-%d")
    return _ai_logs_dir() / f"{stamp}_ai_calls.log"


def _atomic_append(path: Path, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = block.encode("utf-8", errors="replace")

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, 0o600)
    try:
        if fcntl is not None:  # pragma: no cover (Linux only)
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        try:
            if fcntl is not None:  # pragma: no cover
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _append_ai_log(block: str) -> None:
    # Ensure each call is appended as one non-interleaving block.
    if not block.endswith("\n"):
        block += "\n"
    with _LOG_LOCK:
        _atomic_append(_ai_log_path(), block)


def _fmt_ai_log_block(
    *,
    provider: str,
    requested_model: str,
    model_version: str,
    tag: str | None,
    prompt: str,
    output_text: str,
    prompt_tokens: int | None,
    output_tokens: int | None,
    elapsed_ms: int | None,
    attempt_notes: list[str],
    error: BaseException | None,
) -> str:
    now = datetime.now(TAIPEI_TIMEZONE).isoformat(timespec="seconds")
    lines: list[str] = []
    lines.append(f"@@AI_CALL_START@@ {now}")
    lines.append(
        "@@AI_META@@ "
        + " ".join(
            p
            for p in (
                f"provider={provider}",
                f"tag={tag or '-'}",
                f"requested_model={requested_model}",
                f"model_version={model_version or '-'}",
                f"prompt_tokens={prompt_tokens if prompt_tokens is not None else '-'}",
                f"output_tokens={output_tokens if output_tokens is not None else '-'}",
                f"elapsed_ms={elapsed_ms if elapsed_ms is not None else '-'}",
            )
            if p
        )
    )
    lines.append("@@INPUT_PROMPT_BEGIN@@")
    lines.append(prompt)
    lines.append("@@INPUT_PROMPT_END@@")

    lines.extend(attempt_notes)

    if error is not None:
        lines.append(f"@@AI_ERROR@@ {type(error).__name__}: {error}")

    lines.append("@@OUTPUT_TEXT_BEGIN@@")
    lines.append(output_text)
    lines.append("@@OUTPUT_TEXT_END@@")
    lines.append("@@AI_CALL_END@@")
    return "\n".join(lines) + "\n"


def _print_ai_telemetry(
    *,
    provider: str,
    tag: str | None,
    requested_model: str,
    model_version: str,
    prompt_tokens: int | None,
    output_tokens: int | None,
    elapsed_ms: int,
    status: str,
) -> None:
    # NOTE: Only emit one-line telemetry to stdout (serial console), never dump prompts.
    print(
        "[ai] "
        f"provider={provider} status={status} tag={tag or '-'} "
        f"model={requested_model} version={model_version or '-'} "
        f"tokens={prompt_tokens if prompt_tokens is not None else '-'}"
        f"/{output_tokens if output_tokens is not None else '-'} "
        f"latency_ms={elapsed_ms}"
    )


def _usage_counts(response: Any) -> tuple[int | None, int | None]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None, None
    prompt = getattr(usage, "prompt_token_count", None)
    out = getattr(usage, "candidates_token_count", None)
    try:
        prompt_int = int(prompt) if prompt is not None else None
    except Exception:
        prompt_int = None
    try:
        out_int = int(out) if out is not None else None
    except Exception:
        out_int = None
    return prompt_int, out_int


# --------------------------------------------------------------------------- #
# 1. Google AI Studio (Free Tier)
# --------------------------------------------------------------------------- #
def get_gemini_model() -> str:
    """Return configured Google AI Studio model from GEMINI_MODEL or default."""
    return (os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()


def _call_google_ai_once(
    prompt: str,
    *,
    model: str | None,
) -> AIResponse:
    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise MissingGeminiApiKeyError("GEMINI_API_KEY is not configured.")

    client = genai.Client(api_key=api_key)
    resolved_model = (model or get_gemini_model()).strip()
    config = types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=GOOGLE_AI_THINKING_BUDGET)
    )
    response = client.models.generate_content(
        model=resolved_model,
        contents=prompt,
        config=config,
    )

    answer = getattr(response, "text", "")
    if not answer:
        raise RuntimeError("Google AI Studio returned an empty response.")

    model_version = str(getattr(response, "model_version", "") or resolved_model)
    prompt_tokens, output_tokens = _usage_counts(response)
    return AIResponse(
        provider="google_ai_studio",
        requested_model=resolved_model,
        model_version=model_version,
        text=answer,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )


_THREAD_LOCAL = threading.local()


def _set_last_ai_meta(meta: AIResponse) -> None:
    _THREAD_LOCAL.last_ai_meta = meta


def _pop_last_ai_meta() -> AIResponse | None:
    meta = getattr(_THREAD_LOCAL, "last_ai_meta", None)
    if meta is not None:
        try:
            delattr(_THREAD_LOCAL, "last_ai_meta")
        except Exception:
            pass
    return meta


def _call_google_ai(prompt: str, model: str | None) -> str:
    return _call_google_ai_once(prompt, model=model).text


def _generate_ai_response(prompt: str, model: str | None = None) -> str:
    """Legacy helper for tests/backward compatibility.

    It still returns text-only, but stores the last response metadata in a thread-local
    so higher-level callers (retry/fallback wrapper) can log model_version/tokens.
    """

    meta = _call_google_ai_once(prompt, model=model)
    _set_last_ai_meta(meta)
    return meta.text


def _generate_with_fallback(prompt: str, model: str | None, fallback_model: str | None) -> str:
    """Call `model` on Google AI Studio; on transient/quota/timeout errors retry or degrade to `fallback_model`.

    This keeps the legacy public behaviour for /ai and generic callers.
    """

    attempt_notes: list[str] = []
    started = time.perf_counter()
    last_meta: AIResponse | None = None
    last_error: BaseException | None = None

    def log_and_return(value: str) -> str:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if last_meta is None:
            return value
        _print_ai_telemetry(
            provider=last_meta.provider,
            tag=None,
            requested_model=last_meta.requested_model,
            model_version=last_meta.model_version,
            prompt_tokens=last_meta.prompt_tokens,
            output_tokens=last_meta.output_tokens,
            elapsed_ms=elapsed_ms,
            status="ok",
        )
        _append_ai_log(
            _fmt_ai_log_block(
                provider=last_meta.provider,
                requested_model=last_meta.requested_model,
                model_version=last_meta.model_version,
                tag=None,
                prompt=prompt,
                output_text=value,
                prompt_tokens=last_meta.prompt_tokens,
                output_tokens=last_meta.output_tokens,
                elapsed_ms=elapsed_ms,
                attempt_notes=attempt_notes,
                error=None,
            )
        )
        return value

    def log_and_raise(error: BaseException) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _print_ai_telemetry(
            provider=(last_meta.provider if last_meta else "google_ai_studio"),
            tag=None,
            requested_model=(last_meta.requested_model if last_meta else (model or get_gemini_model())),
            model_version=(last_meta.model_version if last_meta else ""),
            prompt_tokens=(last_meta.prompt_tokens if last_meta else None),
            output_tokens=(last_meta.output_tokens if last_meta else None),
            elapsed_ms=elapsed_ms,
            status="error",
        )
        _append_ai_log(
            _fmt_ai_log_block(
                provider=(last_meta.provider if last_meta else "google_ai_studio"),
                requested_model=(last_meta.requested_model if last_meta else (model or get_gemini_model())),
                model_version=(last_meta.model_version if last_meta else ""),
                tag=None,
                prompt=prompt,
                output_text="",
                prompt_tokens=(last_meta.prompt_tokens if last_meta else None),
                output_tokens=(last_meta.output_tokens if last_meta else None),
                elapsed_ms=elapsed_ms,
                attempt_notes=attempt_notes,
                error=error,
            )
        )
        raise error

    try:
        answer = _generate_ai_response(prompt, model)
        last_meta = _pop_last_ai_meta()
        return log_and_return(answer)
    except MissingGeminiApiKeyError as error:
        # Legacy behaviour for tests: allow retry wrapper to run even if GEMINI_API_KEY isn't set.
        # When the generator is mocked, it should not depend on env vars.
        if model is None and fallback_model is None:
            try:
                answer = _generate_ai_response(prompt, model)
                last_meta = _pop_last_ai_meta()
                return log_and_return(answer)
            except Exception:
                pass
        raise
    except Exception as error:  # noqa: BLE001
        last_error = error
        if not _is_retryable_gemini_error(error):
            log_and_raise(error)
        target_model = fallback_model if fallback_model is not None else model
        if fallback_model:
            logger.warning(
                "Gemini model %s unavailable (%s); falling back to %s.",
                model,
                error,
                fallback_model,
            )
            attempt_notes.append(
                f"@@AI_FALLBACK@@ from={model or get_gemini_model()} to={fallback_model} reason={type(error).__name__}: {error}"
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
            attempt_notes.append(
                f"@@AI_RETRY@@ model={target_model or get_gemini_model()} sleep={delay} reason={type(last_error).__name__}: {last_error}"
            )
            time.sleep(delay)
            try:
                answer = _generate_ai_response(prompt, target_model)
                last_meta = _pop_last_ai_meta()
                return log_and_return(answer)
            except MissingGeminiApiKeyError:
                raise
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not _is_retryable_gemini_error(error):
                    log_and_raise(error)
        try:
            answer = _generate_ai_response(prompt, model=target_model)
            last_meta = _pop_last_ai_meta()
            return log_and_return(answer)
        except Exception as error:  # noqa: BLE001
            log_and_raise(error)

    for delay in retry_schedule:
        try:
            answer = _generate_ai_response(prompt, model=target_model)
            last_meta = _pop_last_ai_meta()
            return log_and_return(answer)
        except MissingGeminiApiKeyError:
            raise
        except Exception as error:  # noqa: BLE001
            last_error = error
            if delay is None or not _is_retryable_gemini_error(error):
                log_and_raise(error)
            attempt_notes.append(
                f"@@AI_RETRY@@ model={target_model or get_gemini_model()} sleep={delay} reason={type(error).__name__}: {error}"
            )
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


async def ask_gemini_with_meta(
    prompt: str,
    *,
    tag: str | None = None,
    model: str | None = None,
    fallback_model: str | None = None,
    retry_delays_seconds: tuple[float, ...] = _TRANSIENT_RETRY_DELAYS_SECONDS,
    fallback_retry_delays_seconds: tuple[float, ...] = _TRANSIENT_RETRY_DELAYS_SECONDS,
    retryable_predicate: Any = _is_retryable_gemini_error,
) -> AIResponse:
    """Ask Google AI Studio and return full response metadata.

    Used by SASD v3.5.1 paths that must show model_version and write consolidated logs.
    """

    def run() -> AIResponse:
        attempt_notes: list[str] = []
        started = time.perf_counter()
        last_error: BaseException | None = None

        resolved_model = (model or get_gemini_model()).strip()
        resolved_fallback = fallback_model.strip() if fallback_model else None

        def finish(meta: AIResponse, *, error: BaseException | None) -> AIResponse:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            _print_ai_telemetry(
                provider=meta.provider,
                tag=tag,
                requested_model=meta.requested_model,
                model_version=meta.model_version,
                prompt_tokens=meta.prompt_tokens,
                output_tokens=meta.output_tokens,
                elapsed_ms=elapsed_ms,
                status="ok" if error is None else "error",
            )
            _append_ai_log(
                _fmt_ai_log_block(
                    provider=meta.provider,
                    requested_model=meta.requested_model,
                    model_version=meta.model_version,
                    tag=tag,
                    prompt=prompt,
                    output_text=(meta.text if error is None else ""),
                    prompt_tokens=meta.prompt_tokens,
                    output_tokens=meta.output_tokens,
                    elapsed_ms=elapsed_ms,
                    attempt_notes=attempt_notes,
                    error=error,
                )
            )
            if error is not None:
                raise error
            return meta

        # Primary model retries
        meta: AIResponse | None = None
        for delay in (0.0, *retry_delays_seconds):
            if delay:
                attempt_notes.append(
                    f"@@AI_RETRY@@ model={resolved_model} sleep={delay} reason={type(last_error).__name__}: {last_error}"
                )
                time.sleep(delay)
            try:
                meta = _call_google_ai_once(prompt, model=resolved_model)
                return finish(meta, error=None)
            except MissingGeminiApiKeyError:
                raise
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not retryable_predicate(error):
                    # Non-retryable: log and raise
                    base = meta or AIResponse(
                        provider="google_ai_studio",
                        requested_model=resolved_model,
                        model_version="",
                        text="",
                        prompt_tokens=None,
                        output_tokens=None,
                    )
                    return finish(base, error=error)

        # Degrade to fallback model
        if resolved_fallback is None:
            base = meta or AIResponse(
                provider="google_ai_studio",
                requested_model=resolved_model,
                model_version="",
                text="",
                prompt_tokens=None,
                output_tokens=None,
            )
            return finish(base, error=last_error or RuntimeError("Google AI Studio call failed."))

        attempt_notes.append(
            f"@@AI_FALLBACK@@ from={resolved_model} to={resolved_fallback} reason={type(last_error).__name__}: {last_error}"
        )
        for delay in (0.0, *fallback_retry_delays_seconds):
            if delay:
                attempt_notes.append(
                    f"@@AI_RETRY@@ model={resolved_fallback} sleep={delay} reason={type(last_error).__name__}: {last_error}"
                )
                time.sleep(delay)
            try:
                meta = _call_google_ai_once(prompt, model=resolved_fallback)
                return finish(meta, error=None)
            except MissingGeminiApiKeyError:
                raise
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not retryable_predicate(error):
                    base = meta or AIResponse(
                        provider="google_ai_studio",
                        requested_model=resolved_fallback,
                        model_version="",
                        text="",
                        prompt_tokens=None,
                        output_tokens=None,
                    )
                    return finish(base, error=error)

        base = meta or AIResponse(
            provider="google_ai_studio",
            requested_model=resolved_fallback,
            model_version="",
            text="",
            prompt_tokens=None,
            output_tokens=None,
        )
        return finish(base, error=last_error or RuntimeError("Google AI Studio call failed."))

    return await asyncio.to_thread(run)


async def ask_gemini_diagnosis_with_meta(
    prompt: str,
    *,
    tag: str | None = None,
    master: bool = False,
) -> AIResponse:
    """Stage 1 單股診斷。master=True（/stock_ai 大師模式）改走 Vertex AI 的 ARGON 決策模型 (VERTEX_MODEL)。"""
    if master:
        return await ask_vertex_ai_with_meta(prompt, tag=tag)
    return await ask_gemini_with_meta(
        prompt,
        tag=tag,
        model=get_stage1_model(),
        fallback_model=get_stage1_fallback_model(),
        retry_delays_seconds=DIAG_PRIMARY_RETRY_DELAYS_SECONDS,
        fallback_retry_delays_seconds=DIAG_FALLBACK_RETRY_DELAYS_SECONDS,
        retryable_predicate=_is_diag_retryable_error,
    )


# --------------------------------------------------------------------------- #
# 2. Google Cloud Vertex AI (Paid Tier - for 22:00 / 22:30 Portfolio Decision)
# --------------------------------------------------------------------------- #
def get_vertex_model() -> str:
    """Return configured Vertex AI model from .env VERTEX_MODEL (primary authority)."""
    return (os.getenv("VERTEX_MODEL") or DEFAULT_VERTEX_MODEL).strip()


def _call_vertex_ai_once(prompt: str, *, model: str | None = None) -> AIResponse:
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
    answer = getattr(response, "text", "")
    if not answer:
        raise RuntimeError("Vertex AI returned an empty response.")

    model_version = str(getattr(response, "model_version", "") or resolved_model)
    prompt_tokens, output_tokens = _usage_counts(response)
    return AIResponse(
        provider="vertex_ai",
        requested_model=resolved_model,
        model_version=model_version,
        text=answer,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
    )


def _generate_vertex_ai_response(prompt: str, model: str | None = None) -> str:
    """Legacy helper for tests/backward compatibility.

    It still returns text-only, but stores the last response metadata in a thread-local
    so higher-level callers (retry wrapper) can log model_version/tokens.
    """

    meta = _call_vertex_ai_once(prompt, model=model)
    _set_last_ai_meta(meta)
    return meta.text


def _generate_vertex_with_retry(prompt: str, model: str | None = None) -> str:
    """Call Vertex AI with transient-error retries ONLY within Vertex AI.

    Never degrades to free-tier Google AI Studio so portfolio decision quality stays guaranteed.
    """

    attempt_notes: list[str] = []
    started = time.perf_counter()
    last_meta: AIResponse | None = None
    last_error: BaseException | None = None

    resolved_model = (model or get_vertex_model()).strip()

    def log_and_return(value: str) -> str:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if last_meta is None:
            return value
        _print_ai_telemetry(
            provider=last_meta.provider,
            tag=None,
            requested_model=last_meta.requested_model,
            model_version=last_meta.model_version,
            prompt_tokens=last_meta.prompt_tokens,
            output_tokens=last_meta.output_tokens,
            elapsed_ms=elapsed_ms,
            status="ok",
        )
        _append_ai_log(
            _fmt_ai_log_block(
                provider=last_meta.provider,
                requested_model=last_meta.requested_model,
                model_version=last_meta.model_version,
                tag=None,
                prompt=prompt,
                output_text=value,
                prompt_tokens=last_meta.prompt_tokens,
                output_tokens=last_meta.output_tokens,
                elapsed_ms=elapsed_ms,
                attempt_notes=attempt_notes,
                error=None,
            )
        )
        return value

    def log_and_raise(error: BaseException) -> None:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _print_ai_telemetry(
            provider=(last_meta.provider if last_meta else "vertex_ai"),
            tag=None,
            requested_model=(last_meta.requested_model if last_meta else resolved_model),
            model_version=(last_meta.model_version if last_meta else ""),
            prompt_tokens=(last_meta.prompt_tokens if last_meta else None),
            output_tokens=(last_meta.output_tokens if last_meta else None),
            elapsed_ms=elapsed_ms,
            status="error",
        )
        _append_ai_log(
            _fmt_ai_log_block(
                provider=(last_meta.provider if last_meta else "vertex_ai"),
                requested_model=(last_meta.requested_model if last_meta else resolved_model),
                model_version=(last_meta.model_version if last_meta else ""),
                tag=None,
                prompt=prompt,
                output_text="",
                prompt_tokens=(last_meta.prompt_tokens if last_meta else None),
                output_tokens=(last_meta.output_tokens if last_meta else None),
                elapsed_ms=elapsed_ms,
                attempt_notes=attempt_notes,
                error=error,
            )
        )
        raise error

    for delay in (*_TRANSIENT_RETRY_DELAYS_SECONDS, None):
        try:
            answer = _generate_vertex_ai_response(prompt, model=resolved_model)
            last_meta = _pop_last_ai_meta()
            return log_and_return(answer)
        except MissingVertexConfigError:
            raise
        except Exception as error:  # noqa: BLE001
            last_error = error
            if delay is None or not _is_retryable_gemini_error(error):
                log_and_raise(error)
            logger.warning(
                "Vertex AI model %s transient error (%s); retrying in %.1fs...",
                resolved_model,
                error,
                delay,
            )
            attempt_notes.append(
                f"@@AI_RETRY@@ model={resolved_model} sleep={delay} reason={type(error).__name__}: {error}"
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


async def ask_vertex_ai_with_meta(
    prompt: str,
    *,
    tag: str | None = None,
    model: str | None = None,
) -> AIResponse:
    """Ask Vertex AI and return response metadata; never falls back to Google AI Studio."""

    def run() -> AIResponse:
        attempt_notes: list[str] = []
        started = time.perf_counter()
        last_error: BaseException | None = None
        resolved_model = (model or get_vertex_model()).strip()

        for delay in (0.0, *_TRANSIENT_RETRY_DELAYS_SECONDS):
            if delay:
                attempt_notes.append(
                    f"@@AI_RETRY@@ model={resolved_model} sleep={delay} reason={type(last_error).__name__}: {last_error}"
                )
                time.sleep(delay)
            try:
                meta = _call_vertex_ai_once(prompt, model=resolved_model)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                _print_ai_telemetry(
                    provider=meta.provider,
                    tag=tag,
                    requested_model=meta.requested_model,
                    model_version=meta.model_version,
                    prompt_tokens=meta.prompt_tokens,
                    output_tokens=meta.output_tokens,
                    elapsed_ms=elapsed_ms,
                    status="ok",
                )
                _append_ai_log(
                    _fmt_ai_log_block(
                        provider=meta.provider,
                        requested_model=meta.requested_model,
                        model_version=meta.model_version,
                        tag=tag,
                        prompt=prompt,
                        output_text=meta.text,
                        prompt_tokens=meta.prompt_tokens,
                        output_tokens=meta.output_tokens,
                        elapsed_ms=elapsed_ms,
                        attempt_notes=attempt_notes,
                        error=None,
                    )
                )
                return meta
            except MissingVertexConfigError:
                raise
            except Exception as error:  # noqa: BLE001
                last_error = error
                if not _is_retryable_gemini_error(error):
                    break

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _print_ai_telemetry(
            provider="vertex_ai",
            tag=tag,
            requested_model=resolved_model,
            model_version="",
            prompt_tokens=None,
            output_tokens=None,
            elapsed_ms=elapsed_ms,
            status="error",
        )
        _append_ai_log(
            _fmt_ai_log_block(
                provider="vertex_ai",
                requested_model=resolved_model,
                model_version="",
                tag=tag,
                prompt=prompt,
                output_text="",
                prompt_tokens=None,
                output_tokens=None,
                elapsed_ms=elapsed_ms,
                attempt_notes=attempt_notes,
                error=last_error,
            )
        )
        raise last_error or RuntimeError("Vertex AI call failed.")

    return await asyncio.to_thread(run)


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
