import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text


class Phase7BSchedulingMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "phase7b.db"
        self.root = Path(__file__).resolve().parents[1]
        self.env = os.environ.copy()
        self.env["DATABASE_URL"] = f"sqlite:///{self.path.as_posix()}"

    def tearDown(self):
        self.tempdir.cleanup()

    def _alembic(self, command, revision):
        return subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision], cwd=self.root, env=self.env, capture_output=True, text=True)

    def test_phase7b_upgrade_and_downgrade_preserve_existing_rows(self):
        self.assertEqual(self._alembic("upgrade", "20260831_02").returncode, 0)
        engine = create_engine(self.env["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO users (id, email, status, created_at, updated_at) VALUES ('11111111111111111111111111111111', 'phase7b@test', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO instagram_accounts (id, user_id, instagram_user_id, provider, connection_status, created_at, updated_at) VALUES ('22222222222222222222222222222222', '11111111111111111111111111111111', '17841438617459999', 'instagram', 'connected', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
                connection.execute(text("INSERT INTO instagram_content_items (id, instagram_account_id, status, content_type, caption, media_url, created_at, updated_at) VALUES ('33333333333333333333333333333333', '22222222222222222222222222222222', 'ready', 'image', 'existing', 'https://example.com/a.jpg', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"))
        finally:
            engine.dispose()
        self.assertEqual(self._alembic("upgrade", "20260831_03").returncode, 0)
        engine = create_engine(self.env["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                self.assertIn("scheduled_for", {column["name"] for column in inspect(connection).get_columns("instagram_content_items")})
                self.assertIsNone(connection.scalar(text("SELECT scheduled_for FROM instagram_content_items WHERE caption = 'existing'")))
        finally:
            engine.dispose()
        self.assertEqual(self._alembic("downgrade", "20260831_02").returncode, 0)
