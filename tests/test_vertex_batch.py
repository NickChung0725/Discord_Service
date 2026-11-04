"""SASD v5.4.0 Vertex AI Batch (BigQuery) tests (all GCP calls mocked)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

import tests  # noqa: F401
import ai_service
import alignment_ai_picker
from alignment_ai_picker import analyze_candidates, resolve_batch_settings


def _response(text: str) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "modelVersion": "gemini-test-001",
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
    }


def _out_row(custom_id: str, text: str, request: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        custom_id=custom_id,
        stock_id=custom_id,
        task_type="STAGE2_DIAGNOSIS",
        request=request or json.dumps({"contents": [{"role": "user", "parts": [{"text": f"prompt {custom_id}"}]}]}),
        response=json.dumps(_response(text)),
        status="",
    )


def _job(state: str, name: str = "projects/123/locations/us-central1/batchPredictionJobs/456") -> SimpleNamespace:
    return SimpleNamespace(name=name, state=SimpleNamespace(name=state), error="boom", output_info=None)


class VertexBatchEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = {
            "FINMIND_REPORTS_DIR": self._tmp.name,
            "GOOGLE_CLOUD_PROJECT_ID": "proj",
            "GOOGLE_CLOUD_LOCATION": "us-central1",
            "BQ_LOG_ENABLED": "false",
        }
        self._env = patch.dict(os.environ, env)
        self._env.start()

        self.create = MagicMock(return_value=_job("JOB_STATE_PENDING"))
        self.get = MagicMock()
        self.clients: list[dict] = []
        outer = self

        def make_client(**kwargs):
            outer.clients.append(kwargs)
            return SimpleNamespace(batches=SimpleNamespace(create=outer.create, get=outer.get))

        self.dataset = SimpleNamespace(default_table_expiration_ms=None)
        self.bq = MagicMock()
        self.bq.get_dataset.return_value = self.dataset
        self.bq.update_dataset.side_effect = lambda ds, fields: ds
        self.bq.get_table.return_value = SimpleNamespace(expires=None)
        self.output_rows = [_out_row("2357", "answer B"), _out_row("2330", "answer A")]
        self.bq.query.return_value.result.return_value = self.output_rows

        patches = [
            patch.object(ai_service.genai, "Client", side_effect=make_client),
            patch("google.cloud.bigquery.Client", return_value=self.bq),
            patch.object(ai_service.time, "sleep", new=lambda *_a, **_k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)

    def _run(self, **kwargs):
        return asyncio.run(
            ai_service.run_vertex_batch_requests(
                {"2330": "prompt A", "2357": "prompt B"}, model="m", poll_interval_seconds=0.0, **kwargs
            )
        )

    def test_empty_requests_returns_empty(self) -> None:
        self.assertEqual(asyncio.run(ai_service.run_vertex_batch_requests({})), {})
        self.create.assert_not_called()

    def test_staging_dataset_expiration_is_five_days(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        self._run()
        self.assertEqual(self.dataset.default_table_expiration_ms, 432000000)
        self.bq.update_dataset.assert_called_once()

    def test_input_and_dest_uri_share_job_seq_id(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        self._run()
        kwargs = self.create.call_args.kwargs
        m_in = re.fullmatch(r"bq://proj\.vertex_batch_staging\.input_(\d{8}_\d{6}_[0-9a-f]{4})", kwargs["src"])
        self.assertIsNotNone(m_in)
        self.assertEqual(kwargs["config"].dest, f"bq://proj.vertex_batch_staging.predictions_{m_in.group(1)}")

    def test_input_rows_and_alignment_with_reversed_output(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        results = self._run(temperature=0.2, task_type="STAGE2_DIAGNOSIS")
        self.assertEqual(results["2330"].text, "answer A")
        self.assertEqual(results["2357"].text, "answer B")
        self.assertEqual(results["2330"].model_version, "gemini-test-001")
        self.assertEqual(results["2330"].prompt_tokens, 10)

        first_load = self.bq.load_table_from_json.call_args_list[0]
        rows = first_load.args[0]
        self.assertEqual([r["custom_id"] for r in rows], ["2330", "2357"])
        self.assertEqual(rows[0]["task_type"], "STAGE2_DIAGNOSIS")
        req = json.loads(rows[0]["request"])
        self.assertEqual(req["contents"][0]["parts"][0]["text"], "prompt A")
        self.assertEqual(req["generationConfig"], {"temperature": 0.2})

    def test_only_batch_load_is_used_and_permanent_log_appended(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        self._run()
        self.bq.insert_rows_json.assert_not_called()
        self.assertEqual(self.bq.load_table_from_json.call_count, 2)  # input table + permanent log
        perm = self.bq.load_table_from_json.call_args_list[1]
        self.assertEqual(perm.args[1], "proj.discord_service.ai_prompt_logs")
        records = perm.args[0]
        self.assertEqual({r["custom_id"] for r in records}, {"2330", "2357"})
        self.assertTrue(all(r["call_mode"] == "VERTEX_BATCH" for r in records))

    def test_query_uses_leading_commas(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        self._run()
        sql = self.bq.query.call_args.args[0]
        self.assertIn("  , stock_id", sql)
        self.assertIn("`proj.vertex_batch_staging.predictions_", sql)

    def test_polling_state_transitions(self) -> None:
        self.get.side_effect = [
            _job("JOB_STATE_PENDING"),
            _job("JOB_STATE_RUNNING"),
            _job("JOB_STATE_SUCCEEDED"),
        ]
        results = self._run()
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(set(results), {"2330", "2357"})

    def test_failed_state_raises(self) -> None:
        self.get.return_value = _job("JOB_STATE_FAILED")
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        self.assertIn("JOB_STATE_FAILED", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

    def test_permanent_log_failure_is_non_blocking(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        calls = {"n": 0}

        def load(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise ConnectionError("bq down")
            return MagicMock()

        self.bq.load_table_from_json.side_effect = load
        results = self._run()
        self.assertEqual(set(results), {"2330", "2357"})

    def test_extract_prompt_log_record(self) -> None:
        request = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hello"}]}]})
        resp = json.dumps(
            {
                "candidates": [{"content": {"parts": [{"text": "abc"}, {"text": "def"}]}}],
                "modelVersion": "gemini-test-001",
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
            }
        )
        rec = ai_service.extract_prompt_log_record(
            {"custom_id": "c1", "stock_id": "2330", "task_type": "T", "request": request, "response": resp},
            duration_sec=1.2345,
            created_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
            default_model="m",
        )
        self.assertEqual(rec["prompt_text"], "hello")
        self.assertEqual(rec["response_text"], "abc\ndef")
        self.assertEqual(rec["response_raw"], resp)
        self.assertEqual(rec["duration_sec"], 1.23)
        self.assertEqual((rec["prompt_tokens"], rec["completion_tokens"], rec["total_tokens"]), (10, 5, 15))
        self.assertEqual(rec["model_name"], "gemini-test-001")
        self.assertEqual(len(rec), 13)

    def test_model_getters_priority(self) -> None:
        with patch.dict(
            os.environ,
            {"VERTEX_BATCH_STAGE1_MODEL": "", "VERTEX_STAGE1_MODEL": "s1", "VERTEX_BATCH_STAGE2_MODEL": "b2"},
        ):
            self.assertEqual(ai_service.get_vertex_batch_stage1_model(), "s1")
            self.assertEqual(ai_service.get_vertex_batch_stage2_model(), "b2")
        with patch.dict(os.environ, {"VERTEX_BATCH_ENABLED": "false"}):
            self.assertFalse(ai_service.is_vertex_batch_enabled())
            self.assertTrue(ai_service.is_vertex_batch_enabled(True))


class _Analyzer:
    def __init__(self, cached_ids: set[str]) -> None:
        self.cached_ids = cached_ids

    async def prepare_diagnosis_context(self, stock_id, *, force_ai=False, publish_war_room=True):
        if stock_id in self.cached_ids:
            return {"cached": True, "report": SimpleNamespace(summary_json="cached", ai_text="t", cached=True)}
        return {"cached": False, "prompt": f"prompt-{stock_id}", "stock_id": stock_id}

    async def finalize_diagnosis_from_ai(self, stock_id, prep, ai_meta, *, publish_war_room=True):
        return SimpleNamespace(summary_json=ai_meta.text, ai_text=ai_meta.text, cached=False)

    def build_quant_snapshot(self, stock_id, *, close=None):
        return f"snap-{stock_id}"


class AnalyzeCandidatesBatchTests(unittest.TestCase):
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"stock_id": s, "stock_name": s, "close": 10.0} for s in ("1101", "2330", "2357")]
        )

    def test_cache_hit_mixed_with_batch(self) -> None:
        cache = MagicMock()
        batch = AsyncMock(
            return_value={"2330": SimpleNamespace(text="ai-2330", model_version="v")}
        )
        with patch.object(alignment_ai_picker, "run_vertex_batch_requests", batch):
            results, failures = asyncio.run(
                analyze_candidates(
                    self._frame(),
                    cache,
                    _Analyzer({"1101"}),
                    force_ai=False,
                    publish_all=False,
                    call_interval=0,
                    use_vertex_batch=True,
                    target_date=date(2026, 11, 3),
                )
            )
        sent = batch.await_args.args[0]
        self.assertEqual(sent, {"2330": "prompt-2330", "2357": "prompt-2357"})
        self.assertEqual(set(results), {"1101", "2330"})  # 2357 missing -> isolated failure
        self.assertEqual(results["2330"]["summary"], "ai-2330")
        self.assertEqual([f[0] for f in failures], ["2357"])
        self.assertIn("Vertex Batch 未回傳", failures[0][1])

    def test_all_cached_skips_batch(self) -> None:
        batch = AsyncMock()
        with patch.object(alignment_ai_picker, "run_vertex_batch_requests", batch):
            results, failures = asyncio.run(
                analyze_candidates(
                    self._frame(),
                    MagicMock(),
                    _Analyzer({"1101", "2330", "2357"}),
                    force_ai=False,
                    publish_all=False,
                    call_interval=0,
                    use_vertex_batch=True,
                )
            )
        batch.assert_not_awaited()
        self.assertEqual(len(results), 3)
        self.assertEqual(failures, [])

    def test_resolve_batch_settings_priority(self) -> None:
        with patch.dict(os.environ, {"VERTEX_BATCH_ENABLED": "true"}):
            self.assertEqual(resolve_batch_settings({}, False)[:2], (False, False))
            self.assertEqual(
                resolve_batch_settings({"ai_picker_use_vertex_batch": False}, None)[:2], (False, True)
            )
            self.assertEqual(resolve_batch_settings({"vertex_batch_poll_interval_seconds": 5}, None)[2], 5.0)


if __name__ == "__main__":
    unittest.main()
