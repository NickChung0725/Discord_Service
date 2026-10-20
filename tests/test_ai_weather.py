from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import ai_weather


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

        report = ai_weather.format_weather_report(weather_data, now=taipei_now)

        self.assertIn("台北今日天氣", report)
        self.assertIn("（2026-10-11）", report)
        self.assertIn("多雲時晴", report)
        self.assertIn("最高 30.0°C／最低 24.5°C", report)
        self.assertIn("降雨機率：60%", report)
        self.assertIn("日出 05:48｜日落 17:34", report)

    def test_weather_fetch_uses_taipei_and_raises_for_http_errors(self) -> None:
        response = Mock()
        response.json.return_value = {"current": {}, "daily": {}}
        with patch("ai_weather.requests.get", return_value=response) as get_request:
            result = ai_weather._fetch_weather_data()

        self.assertEqual(result, {"current": {}, "daily": {}})
        self.assertEqual(get_request.call_args.kwargs["params"]["timezone"], "Asia/Taipei")
        self.assertEqual(get_request.call_args.kwargs["params"]["latitude"], 25.033)
        response.raise_for_status.assert_called_once()

    def test_invalid_weather_data_fails_explicitly(self) -> None:
        with self.assertRaisesRegex(ValueError, "incomplete or invalid"):
            ai_weather.format_weather_report({"current": {}, "daily": {}})

    def test_gemini_requires_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ai_weather.MissingGeminiApiKeyError):
                ai_weather._generate_ai_response("hello")

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
            patch("ai_weather.genai.Client", return_value=client) as create_client,
        ):
            answer = ai_weather._generate_ai_response("hello")

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
            patch("ai_weather.genai.Client", return_value=client),
        ):
            answer = ai_weather._generate_ai_response("hello")

        self.assertEqual(answer, "Answer from Gemini")
        self.assertEqual(
            client.interactions.create.call_args.kwargs["model"],
            "gemini-flash-lite-latest",
        )

    def test_long_ai_answers_are_split_without_loss(self) -> None:
        answer = "a" * 4001

        messages = ai_weather._discord_messages(answer)

        self.assertEqual([len(message) for message in messages], [2000, 2000, 1])
        self.assertEqual("".join(messages), answer)
