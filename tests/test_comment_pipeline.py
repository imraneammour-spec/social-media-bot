"""Shared comment pipeline (comment_pipeline.py) + both handlers use it.

Covers the shared decisions (parse, own-comment guard, trigger words)
and the legacy handler's stage order through them; the OAuth handler's
own-comment guard is covered with the diagnostic harness. The two
handlers keep separate bodies (different state backends, delivery
modes, error semantics) — see comment_pipeline.py for why.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from comment_pipeline import (
    comment_has_trigger_word,
    comment_words,
    is_own_account_comment,
    parse_comment_event,
)
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    InstagramEngagementEvent,
    User,
)

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


class CommentPipelineHelpersTest(unittest.TestCase):
    def test_parse_normalizes_all_fields(self):
        parsed = parse_comment_event(
            {
                "id": "  c1 ",
                "text": " send the prompt ",
                "from": {"id": "u9", "username": " Alice "},
                "media": {"id": " m1 "},
            }
        )
        self.assertEqual(
            parsed,
            {
                "comment_id": "c1",
                "comment_text": "send the prompt",
                "commenter_id": "u9",
                "username": "Alice",
                "media_id": "m1",
            },
        )

    def test_parse_tolerates_missing_and_malformed_fields(self):
        self.assertEqual(
            parse_comment_event(None),
            {
                "comment_id": "",
                "comment_text": "",
                "commenter_id": "",
                "username": "",
                "media_id": "",
            },
        )
        self.assertEqual(
            parse_comment_event({"from": "not-a-dict", "media": ["x"]})["commenter_id"],
            "",
        )

    def test_trigger_words_match_whole_words_case_insensitively(self):
        trigger = {"ai", "prompt", "send"}
        self.assertTrue(comment_has_trigger_word("Send the prompt please", trigger))
        self.assertTrue(comment_has_trigger_word("AI?", trigger))
        self.assertFalse(comment_has_trigger_word("Amazing work!", trigger))
        self.assertFalse(comment_has_trigger_word("sending love", trigger))
        self.assertFalse(comment_has_trigger_word("hello", set()))
        self.assertEqual(comment_words("Hello, World! 123"), ["hello", "world", "123"])

    def test_own_account_guard_by_id_and_by_username(self):
        self.assertTrue(
            is_own_account_comment("111", "someone", owner_id="111")
        )
        self.assertTrue(
            is_own_account_comment(
                "222", "Imran.Prompts", owner_id="111", owner_username="imran.prompts"
            )
        )
        self.assertFalse(
            is_own_account_comment(
                "222", "alice", owner_id="111", owner_username="imran.prompts"
            )
        )
        # OAuth callers pass no username: id match only.
        self.assertFalse(is_own_account_comment("222", "imran.prompts", owner_id="111"))


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


class LegacyProcessCommentTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir()
        environment = {
            "DATABASE_URL": f"sqlite:///{Path(self.temp_dir.name) / 'w.db'}",
            "FLASK_SECRET_KEY": "pipeline-test-secret",
            "VERIFY_TOKEN": "pipeline-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
            "POSTENZA_EMBEDDED_SCHEDULER": "false",
        }
        self.module_name = f"pipeline_legacy_{uuid.uuid4().hex}"
        self.module = _load_webhook_module(
            environment, self.module_name, self.data_dir
        )

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

    def test_own_comment_is_ignored_without_claim(self):
        self.module.process_comment(
            self._comment("send the prompt", commenter_id="17841438617458289"),
            "https://postenza.app",
        )
        self.assertEqual(self.module.load_processed_comments(), {})
        self.module.process_comment(
            self._comment("send the prompt", username="imran.prompts"),
            "https://postenza.app",
        )
        self.assertEqual(self.module.load_processed_comments(), {})

    def test_non_trigger_comment_goes_to_smart_reply(self):
        import legacy_bot  # process_comment lives here now; patch its namespace

        with patch.object(
            legacy_bot, "process_smart_comment"
        ) as smart:
            self.module.process_comment(
                self._comment("Beautiful work!"), "https://postenza.app"
            )
        smart.assert_called_once_with("c1", "Beautiful work!", "u1", "alice")
        self.assertEqual(self.module.load_processed_comments(), {})

    def test_trigger_comment_without_media_releases_claim(self):
        self.module.process_comment(
            self._comment("send the prompt please", media_id=""),
            "https://postenza.app",
        )
        self.assertEqual(self.module.load_processed_comments(), {})

    def test_trigger_comment_full_prompt_flow(self):
        import legacy_bot  # process_comment + its helpers live here now

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
        self.assertEqual(
            self.module.load_processed_comments(), {"c1": "done"}
        )
        # Cooldown now blocks the same user on the same media.
        self.assertTrue(
            self.module.is_request_on_cooldown("m1", "u1", "alice")
        )


class OauthOwnCommentGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'diag.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "oauth-pipeline-test-secret",
            "META_APP_SECRET": "oauth-pipeline-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "GEMINI_API_KEY": "",
        }
        module_name = f"pipeline_oauth_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread"
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(self.module)
        self.module_name = module_name
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            user = User(email="pipeline@example.test")
            session.add(user)
            session.flush()
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617459901",
                provider="instagram",
                connection_status="connected",
            )
            session.add(account)
            session.flush()
            self.account_id = account.id
            session.add(
                InstagramAutomationConfig(
                    instagram_account_id=account.id,
                    enabled=True,
                    trigger_keywords=["prompt"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    smart_reply_enabled=False,
                )
            )

    def tearDown(self):
        self.Session.kw["bind"].dispose()
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def test_own_comment_returns_before_claim(self):
        from database.account_runtime import InstagramAccountRoute

        route = InstagramAccountRoute(self.account_id, "oauth")
        with patch.object(
            self.module, "claim_oauth_event"
        ) as claim:
            self.module._process_oauth_comment(
                {
                    "id": "c9",
                    "text": "send the prompt",
                    "from": {"id": "17841438617459901", "username": "owner"},
                    "media": {"id": "m1"},
                },
                route,
                "17841438617459901",
                "https://postenza.app",
                self.Session,
            )
        claim.assert_not_called()
        with self.Session() as session:
            self.assertEqual(
                len(session.scalars(select(InstagramEngagementEvent)).all()), 0
            )


if __name__ == "__main__":
    unittest.main()
