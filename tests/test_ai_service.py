from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, patch

import tests  # noqa: F401
import ai_service


class GoogleAiStudioTests(unittest.TestCase):
    def test_gemini_requires_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ai_service.MissingGeminiApiKeyError):
                ai_service._generate_ai_response("hello")

    def test_gemini_uses_configured_key_model_and_prompt(self) -> None:
        response = Mock()
        response.text = "Hello!"
        response.model_version = "test-version"
        response.usage_metadata = Mock(prompt_token_count=1, candidates_token_count=2)
        client = Mock()
        client.models.generate_content.return_value = response
        with (
            patch.dict(
                "os.environ",
                {"GEMINI_API_KEY": "test-key", "AI_MODEL_BASE": "test-model"},
                clear=True,
            ),
            patch("ai_service.genai.Client", return_value=client) as create_client,
        ):
            answer = ai_service._generate_ai_response("hello")

        self.assertEqual(answer, "Hello!")
        create_client.assert_called_once_with(
            api_key="test-key",
            http_options=ai_service.types.HttpOptions(timeout=ai_service.GOOGLE_AI_HTTP_TIMEOUT_MS),
        )
        call = client.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "test-model")
        self.assertEqual(call["contents"], "hello")
        self.assertIn("config", call)

    def test_gemini_uses_working_sample_default_model(self) -> None:
        response = Mock(text="Answer from Gemini", model_version="test-version")
        response.usage_metadata = Mock(prompt_token_count=1, candidates_token_count=2)
        client = Mock()
        client.models.generate_content.return_value = response
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch("ai_service.genai.Client", return_value=client),
        ):
            answer = ai_service._generate_ai_response("hello")

        self.assertEqual(answer, "Answer from Gemini")
        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemini-flash-lite-latest",
        )

    def test_long_ai_answers_are_split_without_loss(self) -> None:
        answer = "a" * 4001

        messages = ai_service._discord_messages(answer)

        self.assertEqual([len(message) for message in messages], [2000, 2000, 1])
        self.assertEqual("".join(messages), answer)

    def test_gemini_retries_504_deadline_exceeded_even_without_fallback_model(self) -> None:
        attempts = 0

        def fake_gen(prompt: str, model: str | None = None) -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError(
                    "Error code: 504 - {'error': {'message': 'Deadline expired before operation could complete.', 'code': 'deadline_exceeded'}}"
                )
            return "recovered free tier"

        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=False),
            patch.object(ai_service, "_generate_ai_response", side_effect=fake_gen),
            patch("ai_service.time.sleep", return_value=None) as sleep_mock,
        ):
            result = asyncio.run(ai_service.ask_gemini("hello"))

        self.assertEqual(result, "recovered free tier")
        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once_with(3.0)


class VertexAiTests(unittest.TestCase):
    def test_vertex_requires_project_id(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ai_service.MissingVertexConfigError):
                ai_service._generate_vertex_ai_response("hello")

    def test_vertex_uses_configured_project_location_api_key_and_model(self) -> None:
        response = Mock(text="Vertex decision result")
        client = Mock()
        client.models.generate_content.return_value = response
        with (
            patch.dict(
                "os.environ",
                {
                    "GOOGLE_CLOUD_PROJECT_ID": "my-gcp-project",
                    "VERTEX_LOCATION": "asia-east1",
                    "VERTEX_API_KEY": "vertex-key-123",
                    "AI_MODEL_MID": "gemini-2.5-pro",
                },
                clear=True,
            ),
            patch("ai_service.genai.Client", return_value=client) as create_client,
        ):
            answer = ai_service._generate_vertex_ai_response("pick stocks")

        self.assertEqual(answer, "Vertex decision result")
        create_client.assert_called_once_with(
            vertexai=True,
            project="my-gcp-project",
            location="asia-east1",
            api_key="vertex-key-123",
        )
        client.models.generate_content.assert_called_once_with(
            model="gemini-2.5-pro",
            contents="pick stocks",
        )

    def test_vertex_location_defaults_to_global_while_bq_uses_google_cloud_location(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GOOGLE_CLOUD_PROJECT_ID": "my-gcp-project",
                "GOOGLE_CLOUD_LOCATION": "us-central1",
            },
            clear=True,
        ):
            self.assertEqual(ai_service.get_vertex_location(), "global")
            self.assertEqual(ai_service.get_bq_location(), "us-central1")

    def test_vertex_retries_transient_error_then_succeeds_without_fallback(self) -> None:
        attempts = 0

        def fake_vertex(prompt: str, model: str | None = None) -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("503 service unavailable")
            return "recovered on vertex"

        with (
            patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT_ID": "p"}, clear=False),
            patch.object(ai_service, "_generate_vertex_ai_response", side_effect=fake_vertex),
            patch("ai_service.time.sleep", return_value=None) as sleep_mock,
            patch.object(ai_service, "_generate_ai_response") as free_tier_mock,
        ):
            result = asyncio.run(ai_service.ask_vertex_ai("prompt", model="gemini-2.5-pro"))

        self.assertEqual(result, "recovered on vertex")
        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once()
        free_tier_mock.assert_not_called()

    def test_vertex_retries_504_deadline_exceeded_error(self) -> None:
        attempts = 0

        def fake_vertex(prompt: str, model: str | None = None) -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError(
                    "Error code: 504 - {'error': {'message': 'Deadline expired before operation could complete.', 'code': 'deadline_exceeded'}}"
                )
            return "recovered after 504"

        with (
            patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT_ID": "p"}, clear=False),
            patch.object(ai_service, "_generate_vertex_ai_response", side_effect=fake_vertex),
            patch("ai_service.time.sleep", return_value=None) as sleep_mock,
        ):
            result = asyncio.run(ai_service.ask_vertex_ai("prompt", model="gemini-2.5-pro"))

        self.assertEqual(result, "recovered after 504")
        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once_with(3.0)

    def test_vertex_raises_when_all_retries_exhausted_and_never_falls_back_to_google_ai(self) -> None:
        with (
            patch.dict("os.environ", {"GOOGLE_CLOUD_PROJECT_ID": "p"}, clear=False),
            patch.object(
                ai_service,
                "_generate_vertex_ai_response",
                side_effect=RuntimeError("429 resource_exhausted"),
            ) as vertex_mock,
            patch("ai_service.time.sleep", return_value=None),
            patch.object(ai_service, "_generate_ai_response") as free_tier_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "429 resource_exhausted"):
                asyncio.run(ai_service.ask_vertex_ai("prompt", model="gemini-2.5-pro"))

        self.assertEqual(vertex_mock.call_count, 4)
        free_tier_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
