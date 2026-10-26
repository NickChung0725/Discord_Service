from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import ai_service
import weather_service


class WeatherReportTests(unittest.TestCase):
    def test_formats_today_weather_in_traditional_chinese(self) -> None:
        weather_data = {
            "current": {
                "weather_code": 2,
                "temperature_2m": 27.4,
                "apparent_temperature": 31.1,
                "relative_humidity_2m": 78,
                "wind_speed_10m": 12.3,
                "precipitation": 0.2,
            },
            "daily": {
                "time": ["2026-10-11"],
                "weather_code": [61],
                "temperature_2m_max": [30.0],
                "temperature_2m_min": [24.5],
                "precipitation_probability_max": [60],
                "sunrise": ["2026-10-11T05:48"],
                "sunset": ["2026-10-11T17:34"],
            },
        }
        taipei_now = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)

        report = weather_service.format_weather_report(weather_data, now=taipei_now)

        self.assertIn("台北今日天氣", report)
        self.assertIn("（2026-10-11）", report)
        self.assertIn("多雲時晴", report)
        self.assertIn("最高 30.0°C／最低 24.5°C", report)
        self.assertIn("降雨機率：60%", report)
        self.assertIn("日出 05:48｜日落 17:34", report)

    def test_weather_fetch_uses_taipei_and_raises_for_http_errors(self) -> None:
        response = Mock()
        response.json.return_value = {"current": {}, "daily": {}}
        with patch("weather_service.requests.get", return_value=response) as get_request:
            result = weather_service._fetch_weather_data()

        self.assertEqual(result, {"current": {}, "daily": {}})
        self.assertEqual(get_request.call_args.kwargs["params"]["timezone"], "Asia/Taipei")
        self.assertEqual(get_request.call_args.kwargs["params"]["latitude"], 25.033)
        response.raise_for_status.assert_called_once()

    def test_invalid_weather_data_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete or invalid"):
            weather_service.format_weather_report({"current": {}, "daily": {}})


class GoogleAiStudioTests(unittest.TestCase):
    def test_gemini_requires_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ai_service.MissingGeminiApiKeyError):
                ai_service._generate_ai_response("hello")

    def test_gemini_uses_configured_key_model_and_prompt(self) -> None:
        response = Mock()
        response.output_text = "Hello!"
        client = Mock()
        client.interactions.create.return_value = response
        with (
            patch.dict(
                "os.environ",
                {"GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model"},
                clear=True,
            ),
            patch("ai_service.genai.Client", return_value=client) as create_client,
        ):
            answer = ai_service._generate_ai_response("hello")

        self.assertEqual(answer, "Hello!")
        create_client.assert_called_once_with(api_key="test-key")
        client.interactions.create.assert_called_once_with(
            model="test-model",
            input="hello",
        )

    def test_gemini_uses_working_sample_default_model(self) -> None:
        response = Mock(output_text="Answer from Gemini")
        client = Mock()
        client.interactions.create.return_value = response
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}, clear=True),
            patch("ai_service.genai.Client", return_value=client),
        ):
            answer = ai_service._generate_ai_response("hello")

        self.assertEqual(answer, "Answer from Gemini")
        self.assertEqual(
            client.interactions.create.call_args.kwargs["model"],
            "gemini-flash-lite-latest",
        )

    def test_long_ai_answers_are_split_without_loss(self) -> None:
        answer = "a" * 4001

        messages = ai_service._discord_messages(answer)

        self.assertEqual([len(message) for message in messages], [2000, 2000, 1])
        self.assertEqual("".join(messages), answer)


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
                    "GOOGLE_CLOUD_LOCATION": "us-central1",
                    "VERTEX_API_KEY": "vertex-key-123",
                    "VERTEX_MODEL": "gemini-2.5-pro",
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
            location="us-central1",
            api_key="vertex-key-123",
        )
        client.models.generate_content.assert_called_once_with(
            model="gemini-2.5-pro",
            contents="pick stocks",
        )

    def test_vertex_retries_transient_error_then_succeeds_without_fallback(self) -> None:
        attempts = 0

        def fake_vertex(prompt: str, model: str | None = None) -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise RuntimeError("503 service unavailable")
            return "recovered on vertex"

        with (
            patch.object(ai_service, "_generate_vertex_ai_response", side_effect=fake_vertex),
            patch("ai_service.time.sleep", return_value=None) as sleep_mock,
            patch.object(ai_service, "_generate_ai_response") as free_tier_mock,
        ):
            result = asyncio.run(ai_service.ask_vertex_ai("prompt", model="gemini-2.5-pro"))

        self.assertEqual(result, "recovered on vertex")
        self.assertEqual(attempts, 2)
        sleep_mock.assert_called_once()
        free_tier_mock.assert_not_called()

    def test_vertex_raises_when_all_retries_exhausted_and_never_falls_back_to_google_ai(self) -> None:
        with (
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
