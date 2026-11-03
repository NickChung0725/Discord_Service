"""SASD v5.3.0 Vertex AI Batch tests (all GCP calls mocked)."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd

import tests  # noqa: F401
import ai_service
import alignment_ai_picker
from alignment_ai_picker import analyze_candidates, resolve_batch_settings


def _record(custom_id: str, text: str) -> str:
    return json.dumps(
        {
            "custom_id": custom_id,
            "response": {
                "candidates": [{"content": {"parts": [{"text": text[:3]}, {"text": text[3:]}]}}],
                "modelVersion": "gemini-test-001",
            },
        }
    )


class _FakeBlob:
    def __init__(self, name: str, data: str = "") -> None:
        self.name = name
        self._data = data
        self.uploaded: str | None = None

    def upload_from_filename(self, path: str) -> None:
        self.uploaded = path

    def download_as_text(self, encoding: str = "utf-8") -> str:
        return self._data


def _job(state: str, name: str = "projects/123/locations/global/batchPredictionJobs/456") -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(name=state),
        error="boom",
        output_info=SimpleNamespace(gcs_output_directory="gs://bkt/vertex_batch/outputs/x/"),
    )


class VertexBatchEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        env = {
            "FINMIND_REPORTS_DIR": self._tmp.name,
            "GOOGLE_CLOUD_PROJECT_ID": "proj",
            "GCS_BUCKET_NAME": "bkt",
        }
        self._env = patch.dict(os.environ, env)
        self._env.start()
        self.clients: list[dict] = []
        self.create = MagicMock(return_value=_job("JOB_STATE_PENDING"))
        self.get = MagicMock()
        outer = self

        def make_client(**kwargs):
            outer.clients.append(kwargs)
            return SimpleNamespace(batches=SimpleNamespace(create=outer.create, get=outer.get))

        self.storage_client = MagicMock()
        self.uploaded_blob = _FakeBlob("up")
        self.storage_client.bucket.return_value.blob.return_value = self.uploaded_blob
        self.output_blobs = [
            _FakeBlob("vertex_batch/outputs/x/inputs/in.jsonl", "ignored"),
            _FakeBlob(
                "vertex_batch/outputs/x/predictions.jsonl",
                "\n".join([_record("2357", "answer B"), _record("2330", "answer A")]),
            ),
        ]
        self.storage_client.list_blobs.return_value = self.output_blobs
        patches = [
            patch.object(ai_service.genai, "Client", side_effect=make_client),
            patch("google.cloud.storage.Client", return_value=self.storage_client),
            patch.object(ai_service.asyncio, "sleep", new=AsyncMock()),
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

    def test_jsonl_packing_and_custom_id_alignment_with_reversed_output(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        results = self._run(temperature=0.2)
        self.assertEqual(results["2330"].text, "answer A")
        self.assertEqual(results["2357"].text, "answer B")
        self.assertEqual(results["2330"].model_version, "gemini-test-001")

        files = list((Path(self._tmp.name) / ".state" / "batch").glob("*_input.jsonl"))
        self.assertEqual(len(files), 1)
        rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["custom_id"] for r in rows], ["2330", "2357"])
        self.assertEqual(rows[0]["request"]["contents"][0]["parts"][0]["text"], "prompt A")
        self.assertEqual(rows[0]["request"]["generationConfig"], {"temperature": 0.2})
        self.assertIsNotNone(self.uploaded_blob.uploaded)

        log_files = list((Path(self._tmp.name) / "logs").glob("*_ai_calls.log"))
        self.assertEqual(len(log_files), 1)
        log = log_files[0].read_text(encoding="utf-8")
        self.assertIn("tag=vertex_batch:2330", log)
        self.assertIn("prompt B", log)

    def test_polling_state_transitions(self) -> None:
        self.get.side_effect = [
            _job("JOB_STATE_PENDING"),
            _job("JOB_STATE_RUNNING"),
            _job("JOB_STATE_SUCCEEDED"),
        ]
        results = self._run()
        self.assertEqual(self.get.call_count, 3)
        self.assertEqual(set(results), {"2330", "2357"})

    def test_dynamic_location_extraction(self) -> None:
        self.get.return_value = _job("JOB_STATE_SUCCEEDED")
        self._run()
        self.assertEqual(self.clients[-1]["location"], "global")
        self.assertTrue(self.clients[-1]["vertexai"])
        self.assertNotIn("api_key", self.clients[-1])

    def test_failed_state_raises(self) -> None:
        self.get.return_value = _job("JOB_STATE_FAILED")
        with self.assertRaises(RuntimeError) as ctx:
            self._run()
        self.assertIn("JOB_STATE_FAILED", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))

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
