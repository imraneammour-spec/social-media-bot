"""Alembic coverage for tenant automation configuration rollout."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


class TenantAutomationMigrationTests(unittest.TestCase):
    previous_revision = "20260827_03"
    revision = "20260829_01"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "automation-migration.db"
        self.environment = os.environ.copy()
        self.environment["DATABASE_URL"] = f"sqlite:///{self.database_path.as_posix()}"
        self.environment["INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED"] = "false"
        self.root = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.temp_dir.cleanup()

    def _alembic(self, command: str, revision: str):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _seed_existing_accounts(self):
        owner_id = "11111111111111111111111111111111"
        eligible_id = "22222222222222222222222222222222"
        unusable_id = "33333333333333333333333333333333"
        legacy_id = "44444444444444444444444444444444"
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, email, status, created_at, updated_at) "
                    "VALUES (:id, :email, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": owner_id, "email": "automation-migration@example.test"})
                for account_id, external_id, status in (
                    (eligible_id, "17841438617458121", "connected"),
                    (unusable_id, "17841438617458122", "connected"),
                    (legacy_id, "17841438617458123", "environment_managed"),
                ):
                    connection.execute(text(
                        "INSERT INTO instagram_accounts "
                        "(id, user_id, instagram_user_id, provider, connection_status, token_expires_at, "
                        "created_at, updated_at) "
                        "VALUES (:id, :user_id, :external_id, 'instagram', :status, "
                        "'2099-01-01 00:00:00', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ), {
                        "id": account_id,
                        "user_id": owner_id,
                        "external_id": external_id,
                        "status": status,
                    })
                for account_id, scopes in (
                    (eligible_id, [
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ]),
                    (unusable_id, ["instagram_business_manage_comments"]),
                ):
                    connection.execute(text(
                        "INSERT INTO instagram_credentials "
                        "(id, instagram_account_id, token_ciphertext, encryption_key_id, scopes, "
                        "expires_at, token_status, created_at, updated_at) "
                        "VALUES (:id, :account_id, 'ciphertext', 'TEST_KEY', :scopes, "
                        "'2099-01-01 00:00:00', 'valid', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ), {
                        "id": "5555555555555555555555555555555" + account_id[-1],
                        "account_id": account_id,
                        "scopes": json.dumps(scopes),
                    })
        finally:
            engine.dispose()
        return eligible_id, unusable_id, legacy_id

    def test_upgrade_backfills_oauth_accounts_without_global_enablement_and_downgrade_is_non_destructive(self):
        """Dropping tenant behavior, enabling unusable accounts, or mutating legacy rows must fail."""

        baseline = self._alembic("upgrade", self.previous_revision)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        eligible_id, unusable_id, legacy_id = self._seed_existing_accounts()

        upgrade = self._alembic("upgrade", "head")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertIn(
                    "instagram_automation_configs", inspect(connection).get_table_names()
                )
                configs = connection.execute(text(
                    "SELECT instagram_account_id, enabled, trigger_keywords, public_reply_enabled, "
                    "public_reply_text FROM instagram_automation_configs"
                )).mappings().all()
        finally:
            engine.dispose()
        by_account = {row["instagram_account_id"]: row for row in configs}
        self.assertEqual(set(by_account), {eligible_id, unusable_id})
        self.assertTrue(bool(by_account[eligible_id]["enabled"]))
        self.assertEqual(
            json.loads(by_account[eligible_id]["trigger_keywords"]),
            ["access", "ai", "prompt", "prompts", "send"],
        )
        self.assertTrue(bool(by_account[eligible_id]["public_reply_enabled"]))
        self.assertEqual(
            by_account[eligible_id]["public_reply_text"],
            "Your prompts are ready. Check your messages.",
        )
        self.assertFalse(bool(by_account[unusable_id]["enabled"]))
        self.assertNotIn(legacy_id, by_account)

        downgrade = self._alembic("downgrade", self.previous_revision)
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertNotIn(
                    "instagram_automation_configs", inspect(connection).get_table_names()
                )
                legacy_status = connection.scalar(text(
                    "SELECT connection_status FROM instagram_accounts WHERE id = :id"
                ), {"id": legacy_id})
                credential_count = connection.scalar(text(
                    "SELECT COUNT(*) FROM instagram_credentials"
                ))
        finally:
            engine.dispose()
        self.assertEqual(legacy_status, "environment_managed")
        self.assertEqual(credential_count, 2)

        reupgrade = self._alembic("upgrade", "head")
        self.assertEqual(reupgrade.returncode, 0, reupgrade.stderr)
