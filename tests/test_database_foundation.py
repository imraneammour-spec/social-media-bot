import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.bootstrap import BootstrapConfigurationError, bootstrap_current_owner
from database.config import database_url_from_environment
from database.models import Base, InstagramAccount, InstagramCredential, User


class DatabaseFoundationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'foundation.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_database_url_is_optional_and_normalizes_railway_postgres_urls(self):
        self.assertIsNone(database_url_from_environment({}))
        self.assertEqual(
            database_url_from_environment({"DATABASE_URL": "postgres://db.example/app"}),
            "postgresql+psycopg://db.example/app",
        )
        self.assertEqual(
            database_url_from_environment({"DATABASE_URL": "postgresql://db.example/app"}),
            "postgresql+psycopg://db.example/app",
        )

    def test_instagram_account_belongs_to_user_and_external_id_is_unique(self):
        with self.Session.begin() as session:
            owner = User(email="owner@example.test", display_name="Owner")
            session.add(owner)
            session.flush()
            account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458289",
                username="owner_account",
            )
            session.add(account)

        with self.Session() as session:
            account = session.scalar(select(InstagramAccount))
            self.assertEqual(account.owner.email, "owner@example.test")

        with self.assertRaises(IntegrityError):
            with self.Session.begin() as session:
                second_owner = User(email="second@example.test")
                session.add(second_owner)
                session.flush()
                session.add(
                    InstagramAccount(
                        user_id=second_owner.id,
                        instagram_user_id="17841438617458289",
                    )
                )

    def test_bootstrap_is_idempotent_and_does_not_create_a_credential(self):
        with self.Session.begin() as session:
            first = bootstrap_current_owner(
                session,
                owner_email="owner@example.test",
                owner_name="Owner",
                instagram_user_id="17841438617458289",
                instagram_username="owner_account",
            )

        with self.Session.begin() as session:
            second = bootstrap_current_owner(
                session,
                owner_email="owner@example.test",
                owner_name="Owner",
                instagram_user_id="17841438617458289",
                instagram_username="owner_account",
            )

        with self.Session() as session:
            self.assertTrue(first.user_created)
            self.assertTrue(first.account_created)
            self.assertFalse(second.user_created)
            self.assertFalse(second.account_created)
            self.assertEqual(len(session.scalars(select(User)).all()), 1)
            self.assertEqual(len(session.scalars(select(InstagramAccount)).all()), 1)
            self.assertEqual(len(session.scalars(select(InstagramCredential)).all()), 0)

    def test_bootstrap_requires_explicit_owner_configuration(self):
        with self.Session.begin() as session:
            with self.assertRaises(BootstrapConfigurationError):
                bootstrap_current_owner(
                    session,
                    owner_email="",
                    owner_name="",
                    instagram_user_id="17841438617458289",
                )

    def test_webhook_initializes_without_database_url_when_scheduler_thread_is_dormant(self):
        """Exercise the application import without allowing its daemon to run."""

        script = """
import os
import threading

class DormantThread:
    def __init__(self, *args, **kwargs):
        pass
    def start(self):
        pass

threading.Thread = DormantThread
os.environ['DATABASE_URL'] = ''
import webhook
assert webhook.DATABASE_URL is None
"""
        environment = os.environ.copy()
        environment["DATABASE_URL"] = ""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_webhook_keeps_json_as_the_live_storage_path(self):
        source = (Path(__file__).resolve().parents[1] / "webhook.py").read_text(
            encoding="utf-8"
        )
        for json_file_constant in (
            "POSTS_FILE",
            "PROCESSED_FILE",
            "PENDING_ACCESS_FILE",
            "REQUESTS_FILE",
            "SMART_REPLY_ACTIVITY_FILE",
            "FOLLOWUPS_FILE",
            "STORIES_FILE",
            "AUTO_STORY_SETTINGS_FILE",
            "DRAFTS_FILE",
        ):
            self.assertIn(json_file_constant, source)
        self.assertIn("load_json_file", source)
        self.assertIn("save_json_file", source)
        self.assertNotIn("from database.models", source)
        self.assertNotIn("from sqlalchemy", source)


if __name__ == "__main__":
    unittest.main()
