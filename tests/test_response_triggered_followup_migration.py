"""Execute the full revision chain and protect populated legacy data on downgrade."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, create_engine, event, inspect, select, text
from sqlalchemy.engine import Engine


class ResponseTriggeredFollowupMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.url = "sqlite:///" + str(Path(self.temp.name) / "migration.db")
        self.environment = patch.dict(os.environ, {"DATABASE_URL": self.url})
        self.environment.start()
        self.addCleanup(self.environment.stop)

        def sqlite_now(connection, _):
            connection.create_function("now", 0, lambda: "2026-09-05 12:00:00")
        event.listen(Engine, "connect", sqlite_now)
        self.addCleanup(event.remove, Engine, "connect", sqlite_now)
        self.config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        command.upgrade(self.config, "20260904_01")
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        metadata = MetaData()
        metadata.reflect(self.engine)
        self.legacy = metadata.tables
        self.account_id = uuid.uuid4().hex
        with self.engine.begin() as conn:
            owner_id = uuid.uuid4().hex
            conn.execute(self.legacy["users"].insert().values(id=owner_id, status="active"))
            conn.execute(self.legacy["instagram_accounts"].insert().values(
                id=self.account_id, user_id=owner_id, instagram_user_id="migration-test",
                connection_status="connected",
            ))
            conn.execute(self.legacy["instagram_prompt_requests"].insert().values(
                id=uuid.uuid4().hex, instagram_account_id=self.account_id,
                media_id="media", comment_id="comment", recipient_id="recipient",
                page_url="https://example.test/prompt",
            ))
            conn.execute(self.legacy["instagram_oauth_followup_jobs"].insert().values(
                id=uuid.uuid4().hex, instagram_account_id=self.account_id,
                source_message_id="legacy-source", recipient_id="legacy-recipient",
                page_url="https://example.test/prompt", run_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            ))
        self.before = self.snapshot()

    def snapshot(self):
        with self.engine.connect() as conn:
            return {name: conn.execute(select(table)).all() for name, table in self.legacy.items()
                    if name != "alembic_version"}

    def assert_revision_and_legacy_intact(self, revision):
        self.assertEqual(self.snapshot(), self.before)
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(text("SELECT version_num FROM alembic_version")), revision)

    def test_populated_upgrade_and_empty_response_state_downgrade_preserve_legacy(self):
        command.upgrade(self.config, "head")
        # Head has moved past 20260907_01 (billing 20261024_01, notifications
        # 20261126_01, legacy-state 20261126_02, trial start 20261127_01,
        # credit lots 20261127_02).
        self.assert_revision_and_legacy_intact("20261127_02")
        inspector = inspect(self.engine)
        table = "instagram_oauth_direct_message_interactions"
        self.assertIn(table, inspector.get_table_names())
        self.assertEqual(inspector.get_foreign_keys(table)[0]["referred_table"], "instagram_accounts")
        self.assertIn(["instagram_account_id", "source_comment_id"],
                      [item["column_names"] for item in inspector.get_unique_constraints(table)])
        self.assertIn(["instagram_account_id", "recipient_id", "recipient_reply_at"],
                      [item["column_names"] for item in inspector.get_indexes(table)])
        column = next(c for c in inspector.get_columns("instagram_oauth_followup_jobs")
                      if c["name"] == "provider_window_expires_at")
        self.assertTrue(column["nullable"])
        with self.engine.connect() as conn:
            self.assertIsNone(conn.scalar(text("SELECT provider_window_expires_at FROM instagram_oauth_followup_jobs")))
        command.downgrade(self.config, "20260904_01")
        self.assertEqual(self.snapshot(), self.before)
        self.assertNotIn(table, inspect(self.engine).get_table_names())
        command.upgrade(self.config, "head")
        self.assert_revision_and_legacy_intact("20261127_02")

    def test_downgrade_refuses_existing_interaction_without_removing_data(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO instagram_oauth_direct_message_interactions "
                "(id, instagram_account_id, source_comment_id, recipient_id, initial_private_reply_at) "
                "VALUES (:id, :account, 'source', 'recipient', '2026-09-05 12:00:00')"
            ), {"id": uuid.uuid4().hex, "account": self.account_id})
        with self.assertRaisesRegex(RuntimeError, "Cannot downgrade while response-triggered follow-up data exists"):
            command.downgrade(self.config, "20260904_01")
        self.assert_revision_and_legacy_intact("20260904_02")
        with self.engine.connect() as conn:
            self.assertEqual(conn.scalar(text("SELECT count(*) FROM instagram_oauth_direct_message_interactions")), 1)

    def test_downgrade_refuses_windowed_job_without_interaction(self):
        command.upgrade(self.config, "head")
        with self.engine.begin() as conn:
            conn.execute(text("UPDATE instagram_oauth_followup_jobs SET provider_window_expires_at = '2026-09-06 12:00:00'"))
        with self.assertRaisesRegex(RuntimeError, "Cannot downgrade while response-triggered follow-up data exists"):
            command.downgrade(self.config, "20260904_01")
        self.assert_revision_and_legacy_intact("20260904_02")
        with self.engine.connect() as conn:
            self.assertIsNotNone(conn.scalar(text("SELECT provider_window_expires_at FROM instagram_oauth_followup_jobs")))
