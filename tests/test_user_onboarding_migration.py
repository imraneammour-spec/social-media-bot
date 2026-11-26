"""Alembic coverage for the additive customer-onboarding profile table."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


class UserOnboardingMigrationTest(unittest.TestCase):
    previous_revision = "20260906_02"
    revision = "20260907_01"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "onboarding-migration.db"
        self.root = Path(__file__).resolve().parents[1]
        self.environment = os.environ.copy()
        self.environment["DATABASE_URL"] = (
            f"sqlite:///{self.database_path.as_posix()}"
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _alembic(self, command, revision):
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _seed_legacy_user(self):
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, status, created_at, updated_at) "
                        "VALUES ('11111111111111111111111111111111', "
                        "'legacy-onboarding@example.test', 'active', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
        finally:
            engine.dispose()

    def test_upgrade_is_additive_nullable_and_does_not_backfill_legacy_users(self):
        baseline = self._alembic("upgrade", self.previous_revision)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        self._seed_legacy_user()

        upgrade = self._alembic("upgrade", self.revision)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)

        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                inspector = inspect(connection)
                self.assertIn("user_onboarding_profiles", inspector.get_table_names())
                columns = {
                    column["name"]: column
                    for column in inspector.get_columns("user_onboarding_profiles")
                }
                self.assertEqual(
                    set(columns),
                    {
                        "user_id",
                        "use_case",
                        "main_goal",
                        "onboarding_source",
                        "selected_plan",
                        "completed_at",
                        "created_at",
                        "updated_at",
                    },
                )
                for name in (
                    "use_case",
                    "main_goal",
                    "onboarding_source",
                    "selected_plan",
                    "completed_at",
                ):
                    self.assertTrue(columns[name]["nullable"], name)
                self.assertFalse(columns["user_id"]["nullable"])
                self.assertFalse(columns["created_at"]["nullable"])
                self.assertFalse(columns["updated_at"]["nullable"])
                foreign_keys = inspector.get_foreign_keys("user_onboarding_profiles")
                self.assertEqual(len(foreign_keys), 1)
                self.assertEqual(foreign_keys[0]["referred_table"], "users")
                self.assertEqual(foreign_keys[0]["referred_columns"], ["id"])
                self.assertEqual(
                    foreign_keys[0].get("options", {}).get("ondelete"), "CASCADE"
                )
                self.assertEqual(
                    connection.scalar(text("SELECT COUNT(*) FROM user_onboarding_profiles")),
                    0,
                )
                self.assertEqual(
                    connection.scalar(text("SELECT COUNT(*) FROM users")), 1
                )
        finally:
            engine.dispose()

    def test_downgrade_drops_only_an_empty_profile_table(self):
        self.assertEqual(self._alembic("upgrade", self.revision).returncode, 0)

        downgrade = self._alembic("downgrade", self.previous_revision)
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)

        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertNotIn(
                    "user_onboarding_profiles", inspect(connection).get_table_names()
                )
                self.assertIn("users", inspect(connection).get_table_names())
        finally:
            engine.dispose()

    def test_downgrade_aborts_when_any_profile_data_exists(self):
        self.assertEqual(self._alembic("upgrade", self.revision).returncode, 0)
        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, status, created_at, updated_at) "
                        "VALUES ('22222222222222222222222222222222', "
                        "'new-onboarding@example.test', 'active', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO user_onboarding_profiles "
                        "(user_id, created_at, updated_at) "
                        "VALUES ('22222222222222222222222222222222', "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    )
                )
        finally:
            engine.dispose()

        downgrade = self._alembic("downgrade", self.previous_revision)
        self.assertNotEqual(downgrade.returncode, 0)
        self.assertIn("Cannot downgrade while onboarding profile data exists", downgrade.stderr)

        engine = create_engine(self.environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertIn(
                    "user_onboarding_profiles", inspect(connection).get_table_names()
                )
                self.assertEqual(
                    connection.scalar(text("SELECT COUNT(*) FROM user_onboarding_profiles")),
                    1,
                )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
