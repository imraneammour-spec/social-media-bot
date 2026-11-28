"""Legacy delivery modes (Prompt / Message / Link) for the legacy bot.

The server-managed account's delivery mode is saved on /app/automations
and read by ``legacy_bot.process_comment``: prompt mode keeps today's
behavior exactly, while direct_message / link send the configured
private message (link appends the destination URL) with no Gemini
prompt extraction. No config row means prompt behavior.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import requests

from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    User,
)
from database.tenant_automation import LEGACY_TRIGGER_KEYWORDS


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return dict(self._payload)


class _FakeRequests:
    """Stand-in for the requests module inside legacy_bot only."""

    RequestException = requests.RequestException

    def __init__(self):
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(200, {"recipient_id": "recipient9"})


class _FlakyRequests(_FakeRequests):
    """First send fails, the retry succeeds."""

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        if len(self.calls) == 1:
            return _FakeResponse(400, {"error": {"message": "template_rejected"}})
        return _FakeResponse(200, {"recipient_id": "recipient9"})


def _load_webhook_module(environment, module_name, data_dir):
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    env = dict(environment)
    env["POSTENZA_DATA_DIR"] = str(data_dir)
    with patch.dict(os.environ, env, clear=False), patch(
        "threading.Thread", _NoopThread
    ):
        sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
        spec.loader.exec_module(module)
    return module


class LegacyDeliveryModesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir()
        environment = {
            "DATABASE_URL": f"sqlite:///{Path(self.temp_dir.name) / 'delivery.db'}",
            "FLASK_SECRET_KEY": "delivery-test-secret",
            "VERIFY_TOKEN": "delivery-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "test-token",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
            "POSTENZA_EMBEDDED_SCHEDULER": "false",
        }
        self.module_name = f"legacy_delivery_{uuid.uuid4().hex}"
        self.module = _load_webhook_module(
            environment, self.module_name, self.data_dir
        )
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            owner = User(email="legacy-delivery@example.test")
            session.add(owner)
            session.flush()
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458289",
                username="imran.prompts",
                connection_status="environment_managed",
            )
            session.add(self.account)

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _comment(self, text, commenter_id="u1", username="alice", media_id="m1"):
        return {
            "id": "c1",
            "text": text,
            "from": {"id": commenter_id, "username": username},
            "media": {"id": media_id},
        }

    def _set_config(self, **fields):
        values = {
            "enabled": True,
            "trigger_keywords": list(LEGACY_TRIGGER_KEYWORDS),
            "public_reply_enabled": True,
            "public_reply_text": "Thanks 🙌 I’ve sent the prompt to your DMs.",
            "delivery_mode": "prompt_request",
            "private_message_text": "Hey 👋 Thanks for your interest! I’ve got something for you.",
            "prompt_opening_message_text": "Hey 👋 Thanks for your interest! Your prompt is ready.",
            "destination_url": "",
            "followup_enabled": True,
            "followup_delay_minutes": 360,
            "followup_message_text": "",
            "smart_reply_enabled": True,
            "smart_reply_tone": "friendly",
            "smart_reply_max_per_hour": 20,
        }
        values.update(fields)
        with self.Session.begin() as session:
            session.add(
                InstagramAutomationConfig(
                    instagram_account_id=self.account.id,
                    **values,
                )
            )

    def test_legacy_trigger_keywords_match_bot(self):
        import legacy_bot

        self.assertEqual(
            set(LEGACY_TRIGGER_KEYWORDS), set(legacy_bot.TRIGGER_WORDS)
        )

    def test_no_config_row_keeps_prompt_flow(self):
        import legacy_bot

        with (
            patch.object(
                legacy_bot,
                "get_or_create_post",
                return_value={"media_id": "m1", "prompts": [{"text": "p"}]},
            ),
            patch.object(
                legacy_bot, "send_public_comment_reply", return_value=True
            ) as public_reply,
            patch.object(
                legacy_bot, "send_access_request", return_value="recipient9"
            ) as access,
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_called_once_with("c1", "alice")
        access.assert_called_once_with("c1")
        self.assertEqual(
            self.module.get_pending_access("recipient9")["page_url"],
            "https://postenza.app/p/m1",
        )
        self.assertEqual(len(self.module.load_requests()), 1)
        self.assertEqual(self.module.load_processed_comments(), {"c1": "done"})

    def test_direct_message_mode_sends_configured_texts_without_extraction(self):
        import legacy_bot

        self._set_config(
            delivery_mode="direct_message",
            public_reply_enabled=True,
            public_reply_text="Thanks! DM sent.",
            private_message_text="Here is your custom message.",
        )
        fake_requests = _FakeRequests()
        with (
            patch.object(
                legacy_bot,
                "get_or_create_post",
                side_effect=AssertionError("must not extract prompts"),
            ),
            patch.object(
                legacy_bot, "send_comment_reply_text", return_value=True
            ) as public_reply,
            patch.object(legacy_bot, "requests", fake_requests),
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_called_once_with("c1", "Thanks! DM sent.")
        self.assertEqual(len(fake_requests.calls), 1)
        self.assertEqual(
            fake_requests.calls[0]["json"]["message"]["text"],
            "Here is your custom message.",
        )
        self.assertEqual(
            fake_requests.calls[0]["json"]["recipient"],
            {"comment_id": "c1"},
        )
        self.assertEqual(len(self.module.load_requests()), 1)
        self.assertEqual(self.module.load_processed_comments(), {"c1": "done"})
        self.assertTrue(self.module.is_request_on_cooldown("m1", "u1", "alice"))

    def test_view_prompts_button_sends_prompt_card_without_bare_link(self):
        import legacy_bot

        fake_requests = _FakeRequests()
        with patch.object(legacy_bot, "requests", fake_requests):
            sent = legacy_bot.send_view_prompts_button(
                "recipient9", "https://example.test/p/m1"
            )
        self.assertTrue(sent)
        self.assertEqual(len(fake_requests.calls), 1)
        payload = fake_requests.calls[0]["json"]["message"]["attachment"]["payload"]
        self.assertEqual(payload["template_type"], "generic")
        element = payload["elements"][0]
        self.assertEqual(
            element["title"],
            "Thanks! ✨ Your AI prompts are ready to view and copy.",
        )
        self.assertEqual(element["subtitle"], "Tap the button below to open them.")
        self.assertEqual(element["buttons"][0]["title"], "View AI Prompts")
        self.assertEqual(element["buttons"][0]["url"], "https://example.test/p/m1")
        self.assertNotIn("https://", element["title"])
        self.assertNotIn("https://", element["subtitle"])

    def test_view_prompts_button_falls_back_to_button_template_when_rejected(self):
        import legacy_bot

        fake_requests = _FlakyRequests()
        with patch.object(legacy_bot, "requests", fake_requests):
            sent = legacy_bot.send_view_prompts_button(
                "recipient9", "https://example.test/p/m1"
            )
        self.assertTrue(sent)
        self.assertEqual(len(fake_requests.calls), 2)
        self.assertEqual(
            fake_requests.calls[0]["json"]["message"]["attachment"]["payload"][
                "template_type"
            ],
            "generic",
        )
        fallback = fake_requests.calls[1]["json"]["message"]["attachment"]["payload"]
        self.assertEqual(fallback["template_type"], "button")
        self.assertEqual(
            fallback["text"],
            "Thanks! ✨ Your AI prompts are ready to view and copy.",
        )
        self.assertNotIn("https://", fallback["text"])
        self.assertEqual(fallback["buttons"][0]["url"], "https://example.test/p/m1")

    def test_direct_message_mode_skips_public_reply_when_disabled(self):
        import legacy_bot

        self._set_config(
            delivery_mode="direct_message",
            public_reply_enabled=False,
            public_reply_text="Thanks! DM sent.",
            private_message_text="Here is your custom message.",
        )
        fake_requests = _FakeRequests()
        with (
            patch.object(
                legacy_bot, "send_comment_reply_text", return_value=True
            ) as public_reply,
            patch.object(legacy_bot, "requests", fake_requests),
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_not_called()
        self.assertEqual(len(fake_requests.calls), 1)
        self.assertEqual(
            fake_requests.calls[0]["json"]["message"]["text"],
            "Here is your custom message.",
        )
        self.assertEqual(self.module.load_processed_comments(), {"c1": "done"})

    def test_link_mode_appends_destination_url_to_dm(self):
        import legacy_bot

        self._set_config(
            delivery_mode="link",
            public_reply_enabled=True,
            public_reply_text="Thanks! Link sent.",
            private_message_text="Here is the link you asked for.",
            destination_url="https://example.invalid/guide",
        )
        fake_requests = _FakeRequests()
        with (
            patch.object(
                legacy_bot,
                "get_or_create_post",
                side_effect=AssertionError("must not extract prompts"),
            ),
            patch.object(
                legacy_bot, "send_comment_reply_text", return_value=True
            ) as public_reply,
            patch.object(legacy_bot, "requests", fake_requests),
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_called_once_with("c1", "Thanks! Link sent.")
        self.assertEqual(len(fake_requests.calls), 1)
        self.assertEqual(
            fake_requests.calls[0]["json"]["message"]["text"],
            "Here is the link you asked for.\n\nhttps://example.invalid/guide",
        )
        self.assertEqual(self.module.load_processed_comments(), {"c1": "done"})

    def test_invalid_delivery_mode_falls_back_to_prompt_flow(self):
        import legacy_bot

        self._set_config(delivery_mode="bogus")
        with (
            patch.object(
                legacy_bot,
                "get_or_create_post",
                return_value={"media_id": "m1", "prompts": [{"text": "p"}]},
            ),
            patch.object(
                legacy_bot, "send_public_comment_reply", return_value=True
            ) as public_reply,
            patch.object(
                legacy_bot, "send_access_request", return_value="recipient9"
            ) as access,
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_called_once_with("c1", "alice")
        access.assert_called_once_with("c1")

    def test_missing_account_row_falls_back_to_prompt_flow(self):
        import legacy_bot

        with self.Session.begin() as session:
            session.delete(session.get(InstagramAccount, self.account.id))
        with (
            patch.object(
                legacy_bot,
                "get_or_create_post",
                return_value={"media_id": "m1", "prompts": [{"text": "p"}]},
            ),
            patch.object(
                legacy_bot, "send_public_comment_reply", return_value=True
            ) as public_reply,
            patch.object(
                legacy_bot, "send_access_request", return_value="recipient9"
            ) as access,
        ):
            self.module.process_comment(
                self._comment("send the prompt please"), "https://postenza.app"
            )
        public_reply.assert_called_once_with("c1", "alice")
        access.assert_called_once_with("c1")


if __name__ == "__main__":
    unittest.main()
