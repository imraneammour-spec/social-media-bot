"""Legacy bot state in Postgres (database/legacy_state.py + webhook delegation).

Covers the JSON -> Postgres move for the load-bearing legacy state
(processed comments / prompt posts / prompt requests / pending access):
the store itself on SQLite, the webhook helpers delegating to the store
when a database + legacy account exist, and the JSON fallback when the
database is unavailable.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.legacy_state import (
    LegacyStateError,
    legacy_state_store,
)
from database.models import (
    Base,
    InstagramAccount,
    LegacyPendingAccess,
    LegacyProcessedComment,
    LegacyPromptPost,
    LegacyPromptRequest,
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


def _make_session_factory(tmpdir):
    engine = create_engine(f"sqlite:///{Path(tmpdir) / 'legacy.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        owner = User(email="legacy-owner@example.test", display_name="Legacy Owner")
        session.add(owner)
        session.flush()
        session.add(
            InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458289",
                username="imran.prompts",
                connection_status="environment_managed",
            )
        )
    return factory, engine


class LegacyStateStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.factory, self.engine = _make_session_factory(self.temp_dir.name)
        self.store = legacy_state_store(self.factory, "17841438617458289")
        self.assertIsNotNone(self.store)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_store_returns_none_without_factory_or_account(self):
        self.assertIsNone(legacy_state_store(None, "17841438617458289"))
        self.assertIsNone(legacy_state_store(self.factory, ""))
        self.assertIsNone(legacy_state_store(self.factory, "999999"))

    def test_claim_is_atomic_and_done_release_round_trip(self):
        self.assertTrue(self.store.claim_comment("c1"))
        self.assertFalse(self.store.claim_comment("c1"))
        self.assertEqual(self.store.load_processed_comments(), {"c1": "processing"})
        self.store.mark_comment_done("c1")
        self.assertEqual(self.store.load_processed_comments(), {"c1": "done"})
        self.store.release_comment("c1")
        self.assertEqual(self.store.load_processed_comments(), {})
        self.assertTrue(self.store.claim_comment("c1"))

    def test_claim_prunes_to_newest_cap(self):
        import database.legacy_state as legacy_state

        with patch.object(legacy_state, "MAX_PROCESSED_COMMENTS", 3):
            for comment_id in ("a", "b", "c", "d"):
                self.assertTrue(self.store.claim_comment(comment_id))
            kept = self.store.load_processed_comments()
        self.assertEqual(set(kept), {"b", "c", "d"})

    def test_prompt_post_cache_round_trip(self):
        self.assertIsNone(self.store.get_saved_post("m1"))
        self.store.save_post(
            "m1",
            "https://www.instagram.com/p/x/",
            [{"text": "prompt one"}],
            cover_url="https://cdn/x.jpg",
        )
        saved = self.store.get_saved_post("m1")
        self.assertEqual(saved["permalink"], "https://www.instagram.com/p/x/")
        self.assertEqual(saved["cover_url"], "https://cdn/x.jpg")
        self.assertEqual(saved["prompts"], [{"text": "prompt one"}])
        self.assertEqual(saved["prompt_count"], 1)
        self.assertTrue(saved["created_at"])
        self.assertEqual(set(self.store.load_posts()), {"m1"})
        # Re-saving overwrites instead of duplicating.
        self.store.save_post("m1", "https://www.instagram.com/p/y/", [{"text": "v2"}])
        self.assertEqual(len(self.store.load_posts()), 1)
        self.assertEqual(
            self.store.get_saved_post("m1")["prompts"], [{"text": "v2"}]
        )

    def test_prompt_request_log_and_latest_lookup(self):
        entry = {
            "request_key": "m1:u1",
            "media_id": "m1",
            "comment_id": "c9",
            "commenter_id": "u1",
            "username": "alice",
            "page_url": "https://postenza.app/p/m1",
        }
        self.store.log_prompt_request(entry)
        requests = self.store.load_requests()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["request_key"], "m1:u1")
        self.assertEqual(requests[0]["comment_id"], "c9")
        self.assertTrue(requests[0]["created_at"])
        latest = self.store.latest_request_for_key("m1:u1")
        self.assertEqual(latest["comment_id"], "c9")
        self.assertIsNone(self.store.latest_request_for_key("m1:nobody"))

    def test_prompt_requests_prune_to_newest_cap(self):
        import database.legacy_state as legacy_state

        with patch.object(legacy_state, "MAX_PROMPT_REQUESTS", 2):
            for index in range(3):
                self.store.log_prompt_request(
                    {
                        "request_key": f"m{index}:u1",
                        "media_id": f"m{index}",
                        "comment_id": f"c{index}",
                        "commenter_id": "u1",
                        "username": "alice",
                        "page_url": "https://postenza.app/p/m1",
                    }
                )
            keys = [row["request_key"] for row in self.store.load_requests()]
        self.assertEqual(keys, ["m1:u1", "m2:u1"])

    def test_pending_access_round_trip(self):
        self.assertIsNone(self.store.get_pending_access("u1"))
        self.assertTrue(
            self.store.save_pending_access("u1", "https://postenza.app/p/m1")
        )
        pending = self.store.get_pending_access("u1")
        self.assertEqual(pending["page_url"], "https://postenza.app/p/m1")
        self.assertTrue(pending["created_at"])
        self.assertFalse(self.store.save_pending_access("", "https://x"))
        self.assertFalse(self.store.save_pending_access("u2", ""))
        self.store.clear_pending_access("u1")
        self.assertIsNone(self.store.get_pending_access("u1"))

    def test_broken_factory_raises_legacy_state_error(self):
        from database.legacy_state import LegacyStateStore as Store

        def broken_factory():
            raise RuntimeError("boom")

        broken = Store(broken_factory, "17841438617458289")
        with self.assertRaises(LegacyStateError):
            broken.load_posts()


class WebhookLegacyStateDelegationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name) / "data"
        self.data_dir.mkdir()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'webhook.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "legacy-state-test-secret",
            "VERIFY_TOKEN": "legacy-state-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
            "POSTENZA_DATA_DIR": str(self.data_dir),
            "POSTENZA_EMBEDDED_SCHEDULER": "false",
        }
        self.module_name = "phase_big_webhook_legacy_state"
        self.module = self._load_webhook_module(environment, self.module_name)
        self.session_factory = self.module.app.extensions[
            "phase3a_auth"
        ].session_factory
        Base.metadata.create_all(self.session_factory.kw["bind"])
        self._create_legacy_owner()

    def _load_webhook_module(self, environment, module_name):
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)
        return module

    def _create_legacy_owner(self):
        with self.session_factory.begin() as session:
            owner = User(
                email="legacy-owner@example.test", display_name="Legacy Owner"
            )
            session.add(owner)
            session.flush()
            session.add(
                InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id=self.module.INSTAGRAM_USER_ID,
                    username="imran.prompts",
                    connection_status="environment_managed",
                )
            )

    def tearDown(self):
        self.session_factory.kw["bind"].dispose()
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _reset_store_cache(self):
        # The Postgres-store cache lives in legacy_bot now.
        import legacy_bot

        legacy_bot._LEGACY_STATE_STORE = None
        legacy_bot._LEGACY_STATE_STORE_CHECKED_AT = None

    def test_claim_comment_uses_postgres_and_blocks_duplicates(self):
        self.assertTrue(self.module.claim_comment("c100"))
        self.assertFalse(self.module.claim_comment("c100"))
        with self.session_factory() as session:
            rows = session.scalars(select(LegacyProcessedComment)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].comment_id, "c100")
        self.assertEqual(rows[0].state, "processing")
        # No JSON file was written.
        self.assertFalse((self.data_dir / "processed_comments.json").exists())
        self.module.mark_comment_done("c100")
        with self.session_factory() as session:
            row = session.scalar(
                select(LegacyProcessedComment).where(
                    LegacyProcessedComment.comment_id == "c100"
                )
            )
        self.assertEqual(row.state, "done")
        self.module.release_comment("c100")
        self.assertEqual(self.module.load_processed_comments(), {})

    def test_prompt_post_and_pending_access_use_postgres(self):
        self.module.save_post(
            "m7", "https://www.instagram.com/p/m7/", [{"text": "p"}]
        )
        saved = self.module.get_saved_post("m7")
        self.assertEqual(saved["prompts"], [{"text": "p"}])
        self.assertEqual(saved["prompt_count"], 1)
        with self.session_factory() as session:
            self.assertEqual(
                len(session.scalars(select(LegacyPromptPost)).all()), 1
            )
        self.assertFalse((self.data_dir / "posts.json").exists())

        self.assertTrue(
            self.module.save_pending_access("u7", "https://postenza.app/p/m7")
        )
        pending = self.module.get_pending_access("u7")
        self.assertEqual(pending["page_url"], "https://postenza.app/p/m7")
        with self.session_factory() as session:
            self.assertEqual(
                len(session.scalars(select(LegacyPendingAccess)).all()), 1
            )
        self.module.clear_pending_access("u7")
        self.assertIsNone(self.module.get_pending_access("u7"))

    def test_cooldown_uses_postgres(self):
        self.module.log_prompt_request(
            "m8", "c8", "u8", "bob", "https://postenza.app/p/m8"
        )
        self.assertTrue(self.module.is_request_on_cooldown("m8", "u8", "bob"))
        self.assertFalse(self.module.is_request_on_cooldown("m8", "u9", "carol"))
        with self.session_factory() as session:
            self.assertEqual(
                len(session.scalars(select(LegacyPromptRequest)).all()), 1
            )
        self.assertFalse((self.data_dir / "requests.json").exists())
        # An 11-minute-old request is out of cooldown.
        with self.session_factory.begin() as session:
            row = session.scalar(select(LegacyPromptRequest))
            row.created_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        self.assertFalse(self.module.is_request_on_cooldown("m8", "u8", "bob"))

    def test_database_failure_falls_back_to_json(self):
        self.assertTrue(self.module.claim_comment("c200"))
        # Break the database: drop the legacy tables behind the store.
        with self.session_factory.begin() as session:
            session.execute(__import__("sqlalchemy").text("DROP TABLE legacy_processed_comments"))
        self._reset_store_cache()
        # The claim must still work, now via the JSON file.
        self.assertTrue(self.module.claim_comment("c201"))
        self.assertFalse(self.module.claim_comment("c201"))
        stored = json.loads(
            (self.data_dir / "processed_comments.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("c201", stored)


if __name__ == "__main__":
    unittest.main()
