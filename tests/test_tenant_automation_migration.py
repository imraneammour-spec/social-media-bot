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
                    "public_reply_text, smart_reply_enabled, smart_reply_tone, "
                    "smart_reply_max_per_hour FROM instagram_automation_configs"
                )).mappings().all()
        finally:
            engine.dispose()
        by_account = {row["instagram_account_id"]: row for row in configs}
        self.assertEqual(set(by_account), {eligible_id, unusable_id})
        self.assertTrue(bool(by_account[eligible_id]["enabled"]))
        self.assertEqual(
            json.loads(by_account[eligible_id]["trigger_keywords"]),
            ["send", "info"],
        )
        self.assertTrue(bool(by_account[eligible_id]["public_reply_enabled"]))
        self.assertEqual(
            by_account[eligible_id]["public_reply_text"],
            "Thanks for commenting 🙌 I’ve sent you a DM.",
        )
        self.assertFalse(bool(by_account[unusable_id]["enabled"]))
        for account_id in (eligible_id, unusable_id):
            self.assertFalse(bool(by_account[account_id]["smart_reply_enabled"]))
            self.assertEqual(by_account[account_id]["smart_reply_tone"], "friendly")
            self.assertEqual(by_account[account_id]["smart_reply_max_per_hour"], 20)
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

    def test_phase6a_downgrade_removes_only_smart_reply_columns(self):
        """The new migration must not remove the pre-existing automation configuration table."""

        baseline = self._alembic("upgrade", "20260830_01")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)

        upgrade = self._alembic("upgrade", "20260830_02")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                upgraded_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("instagram_automation_configs")
                }
        finally:
            engine.dispose()
        self.assertTrue({
            "smart_reply_enabled",
            "smart_reply_tone",
            "smart_reply_max_per_hour",
        }.issubset(upgraded_columns))

        downgrade = self._alembic("downgrade", "20260830_01")
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertIn(
                    "instagram_automation_configs", inspect(connection).get_table_names()
                )
                downgraded_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("instagram_automation_configs")
                }
        finally:
            engine.dispose()
        self.assertFalse({
            "smart_reply_enabled",
            "smart_reply_tone",
            "smart_reply_max_per_hour",
        } & downgraded_columns)

    def test_general_keyword_migration_defaults_legacy_configs_and_allows_message_only_followups(self):
        """The additive migration must retain legacy prompt semantics while enabling direct jobs."""

        baseline = self._alembic("upgrade", "20260831_04")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        upgrade = self._alembic("upgrade", "head")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                config_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("instagram_automation_configs")
                }
                followup_columns = {
                    column["name"]
                    for column in inspect(connection).get_columns("instagram_oauth_followup_jobs")
                }
                page_url_column = next(
                    column for column in inspect(connection).get_columns("instagram_oauth_followup_jobs")
                    if column["name"] == "page_url"
                )
        finally:
            engine.dispose()

        self.assertTrue({
            "delivery_mode",
            "private_message_text",
            "followup_message_text",
        }.issubset(config_columns))
        self.assertIn("message_text", followup_columns)
        self.assertTrue(page_url_column["nullable"])

    def test_v11_link_delivery_migration_adds_nullable_fields_preserves_rows_and_guards_downgrade(self):
        """The V1.1 migration must preserve saved copy and refuse destructive rollback."""

        baseline = self._alembic("upgrade", "20260904_02")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        owner_id = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        account_id = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        config_id = "cccccccccccccccccccccccccccccccc"
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, email, status, created_at, updated_at) "
                    "VALUES (:id, 'v11-migration@example.test', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": owner_id})
                connection.execute(text(
                    "INSERT INTO instagram_accounts "
                    "(id, user_id, instagram_user_id, provider, connection_status, created_at, updated_at) "
                    "VALUES (:id, :user_id, '17841438617458991', 'instagram', 'connected', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": account_id, "user_id": owner_id})
                connection.execute(text(
                    "INSERT INTO instagram_automation_configs "
                    "(id, instagram_account_id, enabled, trigger_keywords, public_reply_enabled, "
                    "public_reply_text, delivery_mode, private_message_text, followup_enabled, "
                    "followup_delay_minutes, followup_message_text, smart_reply_enabled, "
                    "smart_reply_tone, smart_reply_max_per_hour, created_at, updated_at) "
                    "VALUES (:id, :account_id, 1, '[\"hello\"]', 0, '', 'direct_message', "
                    "'Keep this saved message', 1, 60, 'Keep this follow-up', 0, 'friendly', 20, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": config_id, "account_id": account_id})
        finally:
            engine.dispose()

        upgrade = self._alembic("upgrade", "head")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                columns = {
                    column["name"]: column
                    for column in inspect(connection).get_columns("instagram_automation_configs")
                }
                saved = connection.execute(text(
                    "SELECT delivery_mode, private_message_text, followup_message_text, "
                    "prompt_opening_message_text, destination_url "
                    "FROM instagram_automation_configs WHERE id = :id"
                ), {"id": config_id}).mappings().one()
        finally:
            engine.dispose()

        self.assertTrue(columns["prompt_opening_message_text"]["nullable"])
        self.assertTrue(columns["destination_url"]["nullable"])
        self.assertEqual(saved["delivery_mode"], "direct_message")
        self.assertEqual(saved["private_message_text"], "Keep this saved message")
        self.assertEqual(saved["followup_message_text"], "Keep this follow-up")
        self.assertIsNone(saved["prompt_opening_message_text"])
        self.assertIsNone(saved["destination_url"])

        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE instagram_automation_configs "
                    "SET prompt_opening_message_text = 'Keep this opening', "
                    "destination_url = 'https://example.test/guide' WHERE id = :id"
                ), {"id": config_id})
        finally:
            engine.dispose()

        downgrade = self._alembic("downgrade", "20260904_02")
        self.assertNotEqual(downgrade.returncode, 0)
        self.assertIn(
            "Cannot downgrade while V1.1 automation data exists.",
            downgrade.stderr,
        )
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertEqual(
                    connection.scalar(text("SELECT version_num FROM alembic_version")),
                    "20260906_01",
                )
                retained = connection.execute(text(
                    "SELECT prompt_opening_message_text, destination_url "
                    "FROM instagram_automation_configs WHERE id = :id"
                ), {"id": config_id}).mappings().one()
        finally:
            engine.dispose()
        self.assertEqual(retained["prompt_opening_message_text"], "Keep this opening")
        self.assertEqual(retained["destination_url"], "https://example.test/guide")

    def test_mode_settings_migration_is_nullable_additive_and_guards_saved_snapshots(self):
        """Existing rows stay byte-for-byte active while saved inactive modes block data loss."""

        baseline = self._alembic("upgrade", "20260906_01")
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        owner_id = "11111111111111111111111111111111"
        account_id = "22222222222222222222222222222222"
        config_id = "33333333333333333333333333333333"
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, email, status, created_at, updated_at) "
                    "VALUES (:id, 'mode-migration@example.test', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": owner_id})
                connection.execute(text(
                    "INSERT INTO instagram_accounts "
                    "(id, user_id, instagram_user_id, provider, connection_status, created_at, updated_at) "
                    "VALUES (:id, :user_id, '17841438617458099', 'instagram', 'connected', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": account_id, "user_id": owner_id})
                connection.execute(text(
                    "INSERT INTO instagram_automation_configs "
                    "(id, instagram_account_id, enabled, trigger_keywords, public_reply_enabled, "
                    "public_reply_text, delivery_mode, private_message_text, followup_enabled, "
                    "followup_delay_minutes, followup_message_text, smart_reply_enabled, "
                    "smart_reply_tone, smart_reply_max_per_hour, created_at, updated_at) "
                    "VALUES (:id, :account_id, 1, '[\"keep\"]', 0, 'Keep public', "
                    "'direct_message', 'Keep private', 0, 75, 'Keep follow-up', 0, "
                    "'friendly', 20, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ), {"id": config_id, "account_id": account_id})
        finally:
            engine.dispose()

        upgrade = self._alembic("upgrade", "head")
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                columns = {
                    column["name"]: column
                    for column in inspect(connection).get_columns("instagram_automation_configs")
                }
                saved = connection.execute(text(
                    "SELECT trigger_keywords, public_reply_text, delivery_mode, "
                    "private_message_text, followup_enabled, followup_delay_minutes, "
                    "followup_message_text, inactive_mode_settings "
                    "FROM instagram_automation_configs WHERE id = :id"
                ), {"id": config_id}).mappings().one()
        finally:
            engine.dispose()
        self.assertTrue(columns["inactive_mode_settings"]["nullable"])
        self.assertEqual(json.loads(saved["trigger_keywords"]), ["keep"])
        self.assertEqual(saved["public_reply_text"], "Keep public")
        self.assertEqual(saved["delivery_mode"], "direct_message")
        self.assertEqual(saved["private_message_text"], "Keep private")
        self.assertFalse(bool(saved["followup_enabled"]))
        self.assertEqual(saved["followup_delay_minutes"], 75)
        self.assertEqual(saved["followup_message_text"], "Keep follow-up")
        self.assertIsNone(saved["inactive_mode_settings"])

        snapshot = json.dumps({
            "version": 1,
            "modes": {"link": {"destination_url": "https://example.test/keep"}},
        })
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "UPDATE instagram_automation_configs SET inactive_mode_settings = :snapshot "
                    "WHERE id = :id"
                ), {"snapshot": snapshot, "id": config_id})
        finally:
            engine.dispose()

        downgrade = self._alembic("downgrade", "20260906_01")
        self.assertNotEqual(downgrade.returncode, 0)
        self.assertIn("Cannot downgrade while inactive automation mode settings exist.", downgrade.stderr)
