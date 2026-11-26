import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.bootstrap import BootstrapConfigurationError, bootstrap_current_owner
from database.config import database_url_from_environment
from database.models import Base, InstagramAccount, InstagramCredential, User


class DatabaseFoundationTest(unittest.TestCase):
    PHASE3B_ACCOUNT_COLUMNS = {
        "provider",
        "connected_at",
        "disconnected_at",
        "last_connection_error_code",
    }
    PHASE3B_CREDENTIAL_COLUMNS = {
        "token_status",
        "issued_at",
        "last_validated_at",
        "last_refreshed_at",
        "revoked_at",
        "last_error_code",
    }
    PHASE4C_TABLES = {
        "instagram_engagement_events",
        "instagram_prompt_posts",
        "instagram_pending_access",
        "instagram_prompt_requests",
        "instagram_smart_reply_activity",
        "instagram_oauth_followup_jobs",
    }
    PHASE5B_PROMPT_REQUEST_COLUMNS = {
        "lead_status",
        "internal_note",
        "lead_archived_at",
    }
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'foundation.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    @staticmethod
    def _table_columns(database_url: str, table_name: str) -> set[str]:
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                return {
                    column["name"]
                    for column in inspect(connection).get_columns(table_name)
                }
        finally:
            engine.dispose()

    @staticmethod
    def _table_names(database_url: str) -> set[str]:
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                return set(inspect(connection).get_table_names())
        finally:
            engine.dispose()

    @staticmethod
    def _alembic_revision(database_url: str) -> str:
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                return str(connection.scalar(text("SELECT version_num FROM alembic_version")))
        finally:
            engine.dispose()

    @staticmethod
    def _legacy_phase_two_state(
        database_url: str, *, include_provider: bool
    ) -> tuple[dict[str, object], int]:
        provider_select = ", a.provider" if include_provider else ""
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                account = dict(
                    connection.execute(
                        text(
                            "SELECT a.user_id, a.instagram_user_id, a.connection_status"
                            f"{provider_select} "
                            "FROM instagram_accounts AS a"
                        )
                    )
                    .mappings()
                    .one()
                )
                credential_count = int(
                    connection.scalar(text("SELECT COUNT(*) FROM instagram_credentials"))
                )
                return account, credential_count
        finally:
            engine.dispose()

    @staticmethod
    def _run_alembic(
        command: str, revision: str, *, root: Path, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", command, revision],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

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

    def test_account_and_credential_lifecycle_metadata_are_persisted(self):
        """Lifecycle state must remain owned by one account and credential."""

        with self.Session.begin() as session:
            owner = User(email="lifecycle-owner@example.test")
            session.add(owner)
            session.flush()
            account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458000",
                provider="instagram",
                connection_status="disconnected",
                connected_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                disconnected_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
                last_connection_error_code="provider_temporary_error",
            )
            session.add(account)
            session.flush()
            session.add(
                InstagramCredential(
                    instagram_account_id=account.id,
                    token_status="revoked",
                    issued_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                    last_validated_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                    last_refreshed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                    revoked_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
                    last_error_code="provider_temporary_error",
                )
            )

        with self.Session() as session:
            account = session.scalar(select(InstagramAccount))
            credential = session.scalar(select(InstagramCredential))
            self.assertEqual(account.provider, "instagram")
            self.assertEqual(account.connection_status, "disconnected")
            self.assertIsNotNone(account.connected_at)
            self.assertIsNotNone(account.disconnected_at)
            self.assertEqual(account.last_connection_error_code, "provider_temporary_error")
            self.assertEqual(credential.token_status, "revoked")
            self.assertIsNotNone(credential.issued_at)
            self.assertIsNotNone(credential.last_validated_at)
            self.assertIsNotNone(credential.last_refreshed_at)
            self.assertIsNotNone(credential.revoked_at)
            self.assertEqual(credential.last_error_code, "provider_temporary_error")
            self.assertEqual(credential.instagram_account_id, account.id)

    def test_alembic_lifecycle_schema_is_reversible(self):
        """The Phase 3B.1 columns must survive a local upgrade/re-upgrade."""

        database_path = Path(self.temp_dir.name) / "alembic-lifecycle.db"
        environment = os.environ.copy()
        environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        root = Path(__file__).resolve().parents[1]
        phase_two_upgrade = self._run_alembic(
            "upgrade", "20260825_01", root=root, environment=environment
        )
        self.assertEqual(phase_two_upgrade.returncode, 0, phase_two_upgrade.stderr)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260825_01")

        legacy_user_id = "11111111111111111111111111111111"
        legacy_instagram_account_id = "22222222222222222222222222222222"
        legacy_instagram_user_id = "17841438617458999"
        engine = create_engine(environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email, display_name, status, created_at, updated_at) "
                        "VALUES (:id, :email, :display_name, :status, :created_at, :updated_at)"
                    ),
                    {
                        "id": legacy_user_id,
                        "email": "legacy-owner@example.test",
                        "display_name": "Legacy Owner",
                        "status": "active",
                        "created_at": "2026-08-25 00:00:00",
                        "updated_at": "2026-08-25 00:00:00",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO instagram_accounts "
                        "(id, user_id, instagram_user_id, username, connection_status, "
                        "created_at, updated_at) "
                        "VALUES (:id, :user_id, :instagram_user_id, :username, "
                        ":connection_status, :created_at, :updated_at)"
                    ),
                    {
                        "id": legacy_instagram_account_id,
                        "user_id": legacy_user_id,
                        "instagram_user_id": legacy_instagram_user_id,
                        "username": "legacy_owner",
                        "connection_status": "environment_managed",
                        "created_at": "2026-08-25 00:00:00",
                        "updated_at": "2026-08-25 00:00:00",
                    },
                )
        finally:
            engine.dispose()

        phase_two_state, credential_count = self._legacy_phase_two_state(
            environment["DATABASE_URL"], include_provider=False
        )
        self.assertEqual(phase_two_state["user_id"], legacy_user_id)
        self.assertEqual(phase_two_state["instagram_user_id"], legacy_instagram_user_id)
        self.assertEqual(phase_two_state["connection_status"], "environment_managed")
        self.assertEqual(credential_count, 0)

        upgrade = self._run_alembic("upgrade", "20260827_02", root=root, environment=environment)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        account_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_accounts"
        )
        credential_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_credentials"
        )
        self.assertTrue(self.PHASE3B_ACCOUNT_COLUMNS.issubset(account_columns))
        self.assertTrue(self.PHASE3B_CREDENTIAL_COLUMNS.issubset(credential_columns))
        self.assertTrue(self.PHASE4C_TABLES.issubset(self._table_names(environment["DATABASE_URL"])))
        upgraded_state, credential_count = self._legacy_phase_two_state(
            environment["DATABASE_URL"], include_provider=True
        )
        self.assertEqual(upgraded_state["user_id"], legacy_user_id)
        self.assertEqual(upgraded_state["instagram_user_id"], legacy_instagram_user_id)
        self.assertEqual(upgraded_state["connection_status"], "environment_managed")
        self.assertEqual(upgraded_state["provider"], "instagram")
        self.assertEqual(credential_count, 0)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260827_02")

        downgrade = self._run_alembic(
            "downgrade", "20260825_01", root=root, environment=environment
        )
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        account_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_accounts"
        )
        credential_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_credentials"
        )
        self.assertFalse(self.PHASE3B_ACCOUNT_COLUMNS.intersection(account_columns))
        self.assertFalse(self.PHASE3B_CREDENTIAL_COLUMNS.intersection(credential_columns))
        self.assertFalse(self.PHASE4C_TABLES.intersection(self._table_names(environment["DATABASE_URL"])))
        downgraded_state, credential_count = self._legacy_phase_two_state(
            environment["DATABASE_URL"], include_provider=False
        )
        self.assertEqual(downgraded_state["user_id"], legacy_user_id)
        self.assertEqual(downgraded_state["instagram_user_id"], legacy_instagram_user_id)
        self.assertEqual(downgraded_state["connection_status"], "environment_managed")
        self.assertEqual(credential_count, 0)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260825_01")

        reupgrade = self._run_alembic(
            "upgrade", "20260827_02", root=root, environment=environment
        )
        self.assertEqual(reupgrade.returncode, 0, reupgrade.stderr)
        account_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_accounts"
        )
        credential_columns = self._table_columns(
            environment["DATABASE_URL"], "instagram_credentials"
        )
        self.assertTrue(self.PHASE3B_ACCOUNT_COLUMNS.issubset(account_columns))
        self.assertTrue(self.PHASE3B_CREDENTIAL_COLUMNS.issubset(credential_columns))
        self.assertTrue(self.PHASE4C_TABLES.issubset(self._table_names(environment["DATABASE_URL"])))
        reupgraded_state, credential_count = self._legacy_phase_two_state(
            environment["DATABASE_URL"], include_provider=True
        )
        self.assertEqual(reupgraded_state["user_id"], legacy_user_id)
        self.assertEqual(reupgraded_state["instagram_user_id"], legacy_instagram_user_id)
        self.assertEqual(reupgraded_state["connection_status"], "environment_managed")
        self.assertEqual(reupgraded_state["provider"], "instagram")
        self.assertEqual(credential_count, 0)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260827_02")

    def test_alembic_lead_management_schema_is_reversible_and_defaults_existing_rows(self):
        database_path = Path(self.temp_dir.name) / "alembic-lead-management.db"
        environment = os.environ.copy()
        environment["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        root = Path(__file__).resolve().parents[1]
        phase_four_upgrade = self._run_alembic(
            "upgrade", "20260827_02", root=root, environment=environment
        )
        self.assertEqual(phase_four_upgrade.returncode, 0, phase_four_upgrade.stderr)

        user_id = "33333333333333333333333333333333"
        account_id = "44444444444444444444444444444444"
        request_id = "55555555555555555555555555555555"
        engine = create_engine(environment["DATABASE_URL"])
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO users (id, email, status, created_at, updated_at) "
                    "VALUES (:id, :email, 'active', :created_at, :updated_at)"
                ), {"id": user_id, "email": "lead-legacy@example.test", "created_at": "2026-08-27", "updated_at": "2026-08-27"})
                connection.execute(text(
                    "INSERT INTO instagram_accounts "
                    "(id, user_id, instagram_user_id, connection_status, created_at, updated_at) "
                    "VALUES (:id, :user_id, :instagram_user_id, 'connected', :created_at, :updated_at)"
                ), {"id": account_id, "user_id": user_id, "instagram_user_id": "17841438617458991", "created_at": "2026-08-27", "updated_at": "2026-08-27"})
                connection.execute(text(
                    "INSERT INTO instagram_prompt_requests "
                    "(id, instagram_account_id, media_id, comment_id, recipient_id, page_url, created_at, updated_at) "
                    "VALUES (:id, :account_id, 'media', 'comment', 'recipient', 'https://example.test/p/o/legacy', :created_at, :updated_at)"
                ), {"id": request_id, "account_id": account_id, "created_at": "2026-08-27", "updated_at": "2026-08-27"})
        finally:
            engine.dispose()

        upgrade = self._run_alembic("upgrade", "20260827_03", root=root, environment=environment)
        self.assertEqual(upgrade.returncode, 0, upgrade.stderr)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260827_03")
        columns = self._table_columns(environment["DATABASE_URL"], "instagram_prompt_requests")
        self.assertTrue(self.PHASE5B_PROMPT_REQUEST_COLUMNS.issubset(columns))
        engine = create_engine(environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                lead = connection.execute(text(
                    "SELECT lead_status, internal_note, lead_archived_at "
                    "FROM instagram_prompt_requests WHERE id = :id"
                ), {"id": request_id}).mappings().one()
                indexes = inspect(connection).get_indexes("instagram_prompt_requests")
        finally:
            engine.dispose()
        self.assertEqual(lead["lead_status"], "new")
        self.assertIsNone(lead["internal_note"])
        self.assertIsNone(lead["lead_archived_at"])
        self.assertIn(
            ["instagram_account_id", "lead_archived_at", "lead_status", "created_at"],
            [index["column_names"] for index in indexes],
        )

        downgrade = self._run_alembic(
            "downgrade", "20260827_02", root=root, environment=environment
        )
        self.assertEqual(downgrade.returncode, 0, downgrade.stderr)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260827_02")
        columns = self._table_columns(environment["DATABASE_URL"], "instagram_prompt_requests")
        self.assertFalse(self.PHASE5B_PROMPT_REQUEST_COLUMNS.intersection(columns))

        reupgrade = self._run_alembic("upgrade", "20260827_03", root=root, environment=environment)
        self.assertEqual(reupgrade.returncode, 0, reupgrade.stderr)
        self.assertEqual(self._alembic_revision(environment["DATABASE_URL"]), "20260827_03")
        engine = create_engine(environment["DATABASE_URL"])
        try:
            with engine.connect() as connection:
                lead = connection.execute(text(
                    "SELECT lead_status, internal_note, lead_archived_at "
                    "FROM instagram_prompt_requests WHERE id = :id"
                ), {"id": request_id}).mappings().one()
        finally:
            engine.dispose()
        self.assertEqual(lead["lead_status"], "new")
        self.assertIsNone(lead["internal_note"])
        self.assertIsNone(lead["lead_archived_at"])

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
