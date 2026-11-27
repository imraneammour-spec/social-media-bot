"""Postenza v2: replies never stop, and Gemini is vision-only.

With no ``GEMINI_API_KEY`` configured at all:

- a keyword comment gets the account's ready reply (DM mode);
- a non-keyword comment gets the ready generic reply — never silence —
  even when the old ``smart_reply_enabled`` flag is off;
- a Prompt-mode comment whose prompt extraction fails falls back to a
  ready DM message instead of going silent.

Gemini must never be called on any of these paths: the tests patch
``webhook._gemini_generate_content`` with an exploding fake, so any
remaining AI call fails the test instead of silently returning "".
"""

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from comment_pipeline import DEFAULT_GENERIC_REPLY
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    InstagramEngagementEvent,
    InstagramOAuthDirectMessageInteraction,
    InstagramSmartReplyActivity,
    User,
)

ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


def _exploding_gemini(*args, **kwargs):
    raise AssertionError("Gemini must not be called on the reply path")


class V2NoGeminiRepliesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'v2-no-gemini.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "v2-no-gemini-test-secret",
            "META_APP_SECRET": "v2-no-gemini-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "legacy-token-must-not-be-used",
            "GEMINI_API_KEY": "",
        }
        spec = importlib.util.spec_from_file_location(
            "v2_no_gemini_webhook", WEBHOOK_PATH
        )
        self.module = importlib.util.module_from_spec(spec)
        sys.modules["v2_no_gemini_webhook"] = self.module
        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(self.module)
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            user = User(email="v2-no-gemini@example.test")
            session.add(user)
            session.flush()
            self.user_id = user.id
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617458101",
                connection_status="connected",
            )
            session.add(account)
            session.flush()
            self.account_id = account.id
            session.add(
                InstagramCredential(
                    instagram_account_id=account.id,
                    token_ciphertext="test-ciphertext",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                )
            )

    def tearDown(self):
        self.Session.kw["bind"].dispose()
        sys.modules.pop("v2_no_gemini_webhook", None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _route(self, account_id):
        from database.account_runtime import InstagramAccountRoute

        return InstagramAccountRoute(account_id, "oauth")

    def _add_config(self, **overrides):
        values = {
            "instagram_account_id": self.account_id,
            "enabled": True,
            "trigger_keywords": ["send"],
            "public_reply_enabled": True,
            "public_reply_text": "Thanks for commenting 🙌 I’ve sent you a DM.",
            "delivery_mode": "direct_message",
            "private_message_text": "Hey 👋 Thanks for your interest! I’ve got something for you.",
            "smart_reply_enabled": False,
            "smart_reply_tone": "friendly",
            "smart_reply_max_per_hour": 20,
        }
        values.update(overrides)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(**values))

    def _comment_entry(self, comment_id, text, media_id="m1"):
        return {
            "changes": [
                {
                    "field": "comments",
                    "value": {
                        "id": comment_id,
                        "text": text,
                        "from": {"id": "customer-1", "username": "alice"},
                        "media": {"id": media_id},
                    },
                }
            ]
        }

    def _process(self, entry):
        with patch.dict(
            os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False
        ):
            self.module.process_oauth_webhook_entry(
                entry,
                self._route(self.account_id),
                "17841438617458101",
                "https://example.test",
            )

    def _event_state(self, comment_id):
        with self.Session() as session:
            event = session.scalar(
                select(InstagramEngagementEvent).where(
                    InstagramEngagementEvent.instagram_account_id
                    == self.account_id,
                    InstagramEngagementEvent.provider_event_id == comment_id,
                    InstagramEngagementEvent.event_type == "comment",
                )
            )
            return event.state if event is not None else None

    def test_keyword_comment_gets_ready_dm_reply_without_gemini(self):
        self._add_config()
        with (
            patch.object(
                self.module, "_gemini_generate_content",
                side_effect=_exploding_gemini,
            ),
            patch.object(
                self.module, "send_oauth_public_comment_reply", return_value=True
            ) as public_reply,
            patch.object(
                self.module, "send_oauth_private_comment_reply",
                return_value="recipient-1",
            ) as private_reply,
        ):
            self._process(self._comment_entry("c-keyword", "send please"))
        public_reply.assert_called_once()
        self.assertEqual(
            public_reply.call_args.kwargs["message"],
            "Thanks for commenting 🙌 I’ve sent you a DM.",
        )
        private_reply.assert_called_once()
        self.assertEqual(
            private_reply.call_args.kwargs["message"],
            "Hey 👋 Thanks for your interest! I’ve got something for you.",
        )
        self.assertEqual(self._event_state("c-keyword"), "done")

    def test_non_keyword_comment_gets_generic_reply_and_never_stays_silent(self):
        # smart_reply_enabled=False on purpose: the old gate must not
        # silence the reply any more.
        self._add_config(
            trigger_keywords=["prompt"],
            public_reply_enabled=False,
            public_reply_text="",
        )
        with (
            patch.object(
                self.module, "_gemini_generate_content",
                side_effect=_exploding_gemini,
            ),
            patch.object(
                self.module, "send_oauth_public_comment_reply", return_value=True
            ) as public_reply,
        ):
            self._process(self._comment_entry("c-generic", "Beautiful work!"))
        public_reply.assert_called_once()
        self.assertEqual(
            public_reply.call_args.kwargs["message"], DEFAULT_GENERIC_REPLY
        )
        self.assertEqual(self._event_state("c-generic"), "done")
        with self.Session() as session:
            recorded = session.scalars(
                select(InstagramSmartReplyActivity).where(
                    InstagramSmartReplyActivity.instagram_account_id
                    == self.account_id,
                    InstagramSmartReplyActivity.comment_id == "c-generic",
                )
            ).all()
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].reply_text, DEFAULT_GENERIC_REPLY)

    def test_prompt_extraction_failure_falls_back_to_ready_dm(self):
        self._add_config(
            trigger_keywords=["prompt"],
            public_reply_text="Thanks 🙌 I’ve sent the prompt to your DMs.",
            delivery_mode="prompt_request",
            private_message_text="Custom ready DM — here is what you asked for.",
        )
        with (
            patch.object(
                self.module, "_gemini_generate_content",
                side_effect=_exploding_gemini,
            ),
            patch.object(
                self.module,
                "lookup_oauth_media",
                return_value={
                    "id": "m1",
                    "media_type": "IMAGE",
                    "media_url": "https://cdn.example.test/img.jpg",
                    "permalink": "https://www.instagram.com/p/m1/",
                },
            ),
            patch.object(
                self.module, "extract_oauth_prompts", return_value=[]
            ),
            patch.object(
                self.module, "send_oauth_public_comment_reply", return_value=True
            ) as public_reply,
            patch.object(
                self.module, "send_oauth_private_comment_reply",
                return_value="recipient-9",
            ) as private_reply,
        ):
            self._process(self._comment_entry("c-fallback", "prompt please"))
        public_reply.assert_called_once()
        private_reply.assert_called_once()
        self.assertEqual(
            private_reply.call_args.kwargs["message"],
            "Custom ready DM — here is what you asked for.",
        )
        self.assertEqual(self._event_state("c-fallback"), "done")
        with self.Session() as session:
            interactions = session.scalars(
                select(InstagramOAuthDirectMessageInteraction).where(
                    InstagramOAuthDirectMessageInteraction.instagram_account_id
                    == self.account_id,
                    InstagramOAuthDirectMessageInteraction.source_comment_id
                    == "c-fallback",
                )
            ).all()
        self.assertEqual(len(interactions), 1)
        self.assertEqual(interactions[0].recipient_id, "recipient-9")

    def test_content_generation_endpoints_answer_410_without_gemini(self):
        # Called directly: the HTTP routes sit behind the legacy-workspace
        # gate (non-legacy users get 403 before reaching the view), so
        # the disablement itself is asserted on the view functions.
        with self.module.app.test_request_context():
            for view in (
                self.module.generate_dashboard_content,
                self.module.generate_content_planner,
            ):
                with self.subTest(view=view.__name__):
                    payload, status = view()
                    self.assertEqual(status, 410)
                    self.assertIn("disabled in Postenza v2", payload["error"])


class LegacyGenericReplyTest(unittest.TestCase):
    def test_legacy_generate_smart_reply_needs_no_gemini(self):
        import gemini_client
        import legacy_bot

        with patch.object(
            gemini_client,
            "_gemini_generate_content",
            side_effect=_exploding_gemini,
        ):
            reply = legacy_bot.generate_smart_reply(
                "lovely post", username="visitor"
            )
        self.assertEqual(reply, DEFAULT_GENERIC_REPLY)


if __name__ == "__main__":
    unittest.main()
