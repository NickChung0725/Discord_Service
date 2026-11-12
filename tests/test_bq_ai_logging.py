"""Unit tests for centralized BigQuery AI logging guarantees across all AI invocation paths."""

import asyncio
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

import ai_service
from quant import alignment_ai_picker


class BigQueryAiLoggingGuaranteeTests(unittest.TestCase):
    def test_save_to_permanent_logs_enforces_schema(self) -> None:
        mock_bq_client = Mock()
        mock_job = Mock()
        mock_bq_client.load_table_from_json.return_value = mock_job

        records = [
            {
                "custom_id": "TEST_001",
                "created_at": datetime.now().isoformat(),
                "call_mode": "ONLINE_GEMINI",
                "task_type": "UNIT_TEST",
                "stock_id": "2330",
                "model_name": "gemini-3.8-flash",
                "duration_sec": 1.23,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_text": "hello",
                "response_text": "world",
                "response_raw": "{}",
            }
        ]

        dur = ai_service.save_to_permanent_logs(mock_bq_client, "test-proj", records)
        self.assertGreaterEqual(dur, 0.0)
        mock_bq_client.load_table_from_json.assert_called_once()
        args, kwargs = mock_bq_client.load_table_from_json.call_args
        job_config = kwargs.get("job_config")
        self.assertIsNotNone(job_config)
        self.assertEqual(len(job_config.schema), 13)
        schema_by_name = {field.name: field for field in job_config.schema}
        self.assertIn("custom_id", schema_by_name)
        self.assertIn("task_type", schema_by_name)
        self.assertIn("stock_id", schema_by_name)
        self.assertEqual(schema_by_name["created_at"].field_type, "TIMESTAMP")
        self.assertTrue(all(field.mode == "NULLABLE" for field in job_config.schema))

    def test_ask_gemini_generates_and_archives(self) -> None:
        fake_meta = ai_service.AIResponse(
            text="Gemini Answer",
            provider="google_ai_studio",
            requested_model="gemini-flash-lite-latest",
            model_version="gemini-flash-lite-latest",
            prompt_tokens=10,
            output_tokens=5,
        )
        with (
            patch.object(ai_service, "_generate_ai_response", return_value="Gemini Answer"),
            patch.object(ai_service, "_pop_last_ai_meta", return_value=fake_meta),
            patch.object(ai_service, "_archive_online_call") as mock_archive,
        ):
            res = asyncio.run(ai_service.ask_gemini("test prompt"))
            self.assertEqual(res, "Gemini Answer")
            mock_archive.assert_called_once()
            _, kwargs = mock_archive.call_args
            self.assertEqual(kwargs["tag"], "ask_gemini")
            self.assertEqual(kwargs["task_type"], "GENERAL_QA")

    def test_ask_vertex_ai_generates_and_archives(self) -> None:
        fake_meta = ai_service.AIResponse(
            text="Vertex Answer",
            provider="vertex_ai",
            requested_model="gemini-4-argon",
            model_version="gemini-4-argon",
            prompt_tokens=20,
            output_tokens=10,
        )
        with (
            patch.object(ai_service, "_generate_vertex_ai_response", return_value="Vertex Answer"),
            patch.object(ai_service, "_pop_last_ai_meta", return_value=fake_meta),
            patch.object(ai_service, "_archive_online_call") as mock_archive,
        ):
            res = asyncio.run(ai_service.ask_vertex_ai("test vertex prompt"))
            self.assertEqual(res, "Vertex Answer")
            mock_archive.assert_called_once()
            _, kwargs = mock_archive.call_args
            self.assertEqual(kwargs["tag"], "ask_vertex_ai")
            self.assertEqual(kwargs["task_type"], "GENERAL_QA")

    def test_run_portfolio_selection_online_passes_metadata_and_blocking(self) -> None:
        fake_resp = ai_service.AIResponse(
            text='{"top_picks": []}',
            provider="vertex_ai",
            requested_model="gemini-4-argon",
            model_version="gemini-4-argon",
            prompt_tokens=100,
            output_tokens=20,
        )
        with patch.object(alignment_ai_picker, "ask_vertex_ai_with_meta", return_value=fake_resp) as mock_ask:
            meta = asyncio.run(
                alignment_ai_picker.run_portfolio_selection(
                    ["dossier 1"],
                    max_top=3,
                    use_vertex_batch=False,
                )
            )
            self.assertEqual(meta, fake_resp)
            mock_ask.assert_called_once()
            _, kwargs = mock_ask.call_args
            self.assertEqual(kwargs["task_type"], "STAGE2_PORTFOLIO")
            self.assertEqual(kwargs["stock_id"], "PORTFOLIO")
            self.assertTrue(kwargs["blocking_bq_log"])
            self.assertTrue(str(kwargs["custom_id"]).startswith("STAGE2_PORTFOLIO_"))

    def test_audit_batch_passes_metadata_and_blocking(self) -> None:
        fake_resp = ai_service.AIResponse(
            text='{"audited_stocks": []}',
            provider="vertex_ai",
            requested_model="gemini-4-argon",
            model_version="gemini-4-argon",
            prompt_tokens=50,
            output_tokens=15,
        )
        with patch.object(alignment_ai_picker, "ask_vertex_ai_with_meta", return_value=fake_resp) as mock_ask:
            items, missing = asyncio.run(
                alignment_ai_picker._audit_batch([("2330", "block1"), ("2454", "block2")], model=None)
            )
            mock_ask.assert_called_once()
            _, kwargs = mock_ask.call_args
            self.assertEqual(kwargs["task_type"], "RED_BLUE_AUDIT")
            self.assertEqual(kwargs["stock_id"], "2330,2454")
            self.assertTrue(kwargs["blocking_bq_log"])
            self.assertTrue(str(kwargs["custom_id"]).startswith("AUDIT_"))


if __name__ == "__main__":
    unittest.main()
