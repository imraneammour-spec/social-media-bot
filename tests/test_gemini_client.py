"""Gemini client retry + fallback regressions.

Covers ``webhook._gemini_generate_with_retry`` (transient 503s retried,
permanent errors raised at once) and ``webhook._gemini_generate_content``
(primary model, then the fallback model), plus the callers that must route
through the helper: ``generate_smart_reply``, ``extract_prompts_with_gemini``
and ``generate_oauth_lead_intelligence``.

The real ``google.genai.errors.ServerError`` / ``ClientError`` types are
used so the transient check in ``_gemini_generate_with_retry`` (``"503"``
in the sanitized detail, or a ``ServerError`` type) is exercised for real.
``time.sleep`` is patched in every retry test so the 5s/10s backoff never
runs. The webhook module is loaded the same way as
``tests/test_webhook_auth_integration.py`` (isolated env + noop scheduler
thread) instead of a bare ``import webhook``.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from google.genai import errors as genai_errors

from database.lead_intelligence import LeadIntelligenceContext

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


def _server_error():
    return genai_errors.ServerError(
        503,
        {"error": {"message": "503 UNAVAILABLE: model overloaded", "status": "UNAVAILABLE"}},
    )


def _client_error(code, message):
    return genai_errors.ClientError(
        code, {"error": {"message": message, "status": "ERROR"}}
    )


def _fake_response(text):
    response = MagicMock()
    response.text = text
    return response


class GeminiClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.database_url = "sqlite:///" + str(Path(cls.temp_dir.name) / "test.db")
        cls.environment = {
            "DATABASE_URL": cls.database_url,
            "FLASK_SECRET_KEY": "gemini-client-test-secret",
            "VERIFY_TOKEN": "gemini-client-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
            "POSTENZA_EMBEDDED_SCHEDULER": "false",
            "POSTENZA_DATA_DIR": cls.temp_dir.name,
        }
        cls.module_name = "gemini_client_under_test"
        spec = importlib.util.spec_from_file_location(cls.module_name, MODULE_PATH)
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[cls.module_name] = cls.module
        with patch.dict(os.environ, cls.environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            spec.loader.exec_module(cls.module)
        from database.models import Base

        Base.metadata.create_all(
            cls.module.app.extensions["phase3a_auth"].session_factory.kw["bind"]
        )

    @classmethod
    def tearDownClass(cls):
        cls.module.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        sys.modules.pop(cls.module_name, None)
        cls.temp_dir.cleanup()

    def _mock_gemini_client(self, side_effect):
        client = MagicMock()
        client.models.generate_content = MagicMock(side_effect=side_effect)
        self.module.GEMINI_CLIENT = client
        return client

    def _models_called(self, client):
        return [
            call.kwargs.get("model")
            for call in client.models.generate_content.call_args_list
        ]

    # -- _gemini_generate_with_retry -------------------------------------

    def test_retry_retries_503_then_succeeds(self):
        generate = MagicMock(
            side_effect=[_server_error(), _server_error(), _fake_response("ok")]
        )
        with patch("time.sleep") as slept:
            result = self.module._gemini_generate_with_retry(generate)
        self.assertEqual(result.text, "ok")
        self.assertEqual(generate.call_count, 3)
        self.assertEqual([call.args[0] for call in slept.call_args_list], [5.0, 10.0])

    def test_retry_raises_400_client_error_at_once(self):
        generate = MagicMock(
            side_effect=_client_error(400, "400 INVALID_ARGUMENT: bad request")
        )
        with patch("time.sleep") as slept:
            with self.assertRaises(genai_errors.ClientError):
                self.module._gemini_generate_with_retry(generate)
        self.assertEqual(generate.call_count, 1)
        slept.assert_not_called()

    def test_retry_raises_429_client_error_at_once(self):
        generate = MagicMock(
            side_effect=_client_error(429, "429 RESOURCE_EXHAUSTED: quota exceeded")
        )
        with patch("time.sleep") as slept:
            with self.assertRaises(genai_errors.ClientError):
                self.module._gemini_generate_with_retry(generate)
        self.assertEqual(generate.call_count, 1)
        slept.assert_not_called()

    # -- _gemini_generate_content (primary + fallback) --------------------

    def test_generate_content_falls_back_after_primary_503s(self):
        client = self._mock_gemini_client(
            [_server_error(), _server_error(), _server_error(), _fake_response("fallback ok")]
        )
        with patch("time.sleep"):
            result = self.module._gemini_generate_content("hello")
        self.assertEqual(result.text, "fallback ok")
        models = self._models_called(client)
        self.assertEqual(models.count(self.module.GEMINI_MODEL), 3)
        self.assertEqual(models.count(self.module.GEMINI_FALLBACK_MODEL), 1)

    def test_generate_content_primary_success_never_calls_fallback(self):
        client = self._mock_gemini_client([_fake_response("primary ok")])
        with patch("time.sleep"):
            result = self.module._gemini_generate_content("hello")
        self.assertEqual(result.text, "primary ok")
        self.assertEqual(self._models_called(client), [self.module.GEMINI_MODEL])

    def test_generate_content_400_on_primary_tries_fallback_once_then_raises(self):
        client = self._mock_gemini_client(
            [
                _client_error(400, "400 INVALID_ARGUMENT: bad request"),
                _client_error(400, "400 INVALID_ARGUMENT: bad request"),
            ]
        )
        with patch("time.sleep"):
            with self.assertRaises(genai_errors.ClientError):
                self.module._gemini_generate_content("hello")
        models = self._models_called(client)
        self.assertEqual(models.count(self.module.GEMINI_MODEL), 1)
        self.assertEqual(models.count(self.module.GEMINI_FALLBACK_MODEL), 1)

    # -- callers route through _gemini_generate_content -------------------

    def test_generate_smart_reply_routes_through_helper(self):
        self.module.GEMINI_CLIENT = MagicMock()
        helper = MagicMock(return_value=_fake_response("Thanks so much!"))
        with patch.object(self.module, "_gemini_generate_content", helper):
            reply = self.module.generate_smart_reply("lovely post", username="visitor")
        helper.assert_called_once()
        self.assertEqual(reply, "Thanks so much!")

    def test_extract_prompts_routes_through_helper(self):
        self.module.GEMINI_CLIENT = MagicMock()
        helper = MagicMock(
            return_value=_fake_response(
                '{"prompts": [{"source_index": 1, "text": "a cat on a beach"}]}'
            )
        )
        with (
            patch.object(self.module, "_gemini_generate_content", helper),
            patch.object(
                self.module,
                "download_image_bytes",
                return_value={"data": b"fake-image", "mime_type": "image/jpeg"},
            ),
        ):
            prompts = self.module.extract_prompts_with_gemini(
                [{"media_type": "IMAGE", "media_url": "https://example.invalid/img.jpg"}]
            )
        helper.assert_called_once()
        self.assertEqual(
            prompts, [{"source_index": 1, "text": "a cat on a beach"}]
        )

    def test_generate_oauth_lead_intelligence_routes_through_helper(self):
        self.module.GEMINI_CLIENT = MagicMock()
        helper = MagicMock(
            return_value=_fake_response(
                '{"intent": "interested", "priority": "high", '
                '"summary": "Asked for the prompts twice.", '
                '"suggested_followup": "Send the link again."}'
            )
        )
        context = LeadIntelligenceContext(
            prompt_request_count=2,
            source_post_available=True,
            prompts_prepared=True,
            followup_status="none",
        )
        with patch.object(self.module, "_gemini_generate_content", helper):
            result = self.module.generate_oauth_lead_intelligence(context)
        helper.assert_called_once()
        self.assertEqual(helper.call_args.kwargs["config"].response_mime_type, "application/json")
        self.assertIsNotNone(result)
        self.assertEqual(result.intent, "interested")
        self.assertEqual(result.priority, "high")


if __name__ == "__main__":
    unittest.main()
