import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import SessionTransaction

from database.credentials import credential_keyring_from_environment, load_decrypted_access_token
from database.instagram_connection import (
    InstagramConnectionError,
    persist_discovered_instagram_connection,
)
from database.models import Base, InstagramAccount, InstagramCredential, User
from instagram_oauth import (
    InstagramAccountDiscovery,
    InstagramOAuthTokenResult,
    normalized_expires_in,
)


class InstagramConnectionPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'connection.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.keyring = credential_keyring_from_environment(
            {
                "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": Fernet.generate_key().decode("ascii"),
            }
        )
        self.token = "long-lived-token-must-never-persist-in-plaintext"
        with self.Session.begin() as session:
            owner = User(email="connection-owner@example.test")
            other_owner = User(email="connection-other@example.test")
            session.add_all([owner, other_owner])
            session.flush()
            self.owner_id = owner.id
            self.other_owner_id = other_owner.id

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _result(self, *, expires_in=3600, scopes=("instagram_business_basic",)):
        return InstagramOAuthTokenResult(
            long_lived_access_token=self.token,
            expires_in=expires_in,
            scopes=scopes,
        )

    def _discovery(self, *, instagram_user_id="17841438617458003", username="connected_account"):
        return InstagramAccountDiscovery(
            instagram_user_id=instagram_user_id,
            username=username,
        )

    def _accounts(self):
        with self.Session() as session:
            return session.scalars(select(InstagramAccount)).all()

    def _credentials(self):
        with self.Session() as session:
            return session.scalars(select(InstagramCredential)).all()

    def _persist(self, *, user_id=None, discovery=None, result=None, keyring=None, now=None):
        return persist_discovered_instagram_connection(
            self.Session,
            user_id=user_id or self.owner_id,
            legacy_instagram_user_id="17841438617459999",
            discovery=discovery or self._discovery(),
            token_result=result or self._result(),
            keyring=keyring or self.keyring,
            now=now or datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )

    def test_new_connection_creates_one_owned_account_and_one_encrypted_credential(self):
        connected = self._persist()

        self.assertEqual(connected.user_id, self.owner_id)
        self.assertEqual(connected.connection_status, "connected")
        self.assertEqual(len(self._accounts()), 1)
        credentials = self._credentials()
        self.assertEqual(len(credentials), 1)
        self.assertNotEqual(credentials[0].token_ciphertext, self.token)
        self.assertNotIn(self.token, credentials[0].token_ciphertext)
        self.assertEqual(credentials[0].token_status, "valid")
        self.assertEqual(credentials[0].scopes, ["instagram_business_basic"])
        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), self.token)

    def test_missing_username_creates_a_safe_account_without_inventing_a_value(self):
        connected = self._persist(discovery=self._discovery(username=None))

        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            self.assertIsNone(account.username)
            self.assertEqual(account.connection_status, "connected")

    def test_same_owner_reconnect_updates_one_account_and_credential_atomically(self):
        first = self._persist()
        replacement = "replacement-long-lived-token-must-remain-private"
        self.token = replacement
        second = self._persist(
            discovery=self._discovery(username="renamed_account"),
            result=self._result(expires_in=7200, scopes=None),
            now=datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(len(self._credentials()), 1)
        with self.Session() as session:
            account = session.get(InstagramAccount, first.id)
            credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == first.id)
            )
            self.assertEqual(account.username, "renamed_account")
            self.assertEqual(
                account.token_expires_at.replace(tzinfo=timezone.utc),
                datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(credential.scopes, None)
            self.assertEqual(load_decrypted_access_token(session, account, self.keyring), replacement)

    def test_reconnect_readiness_uses_only_the_scopes_meta_granted(self):
        """Replacing a credential with a denied scope must return to reconnect-required."""
        from database.oauth_capability import assess_oauth_engagement_records

        full_scopes = (
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
        )
        first_now = datetime.now(timezone.utc)
        connected = self._persist(result=self._result(scopes=full_scopes), now=first_now)
        with self.Session() as session:
            account = session.get(InstagramAccount, connected.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == connected.id
            ))
            active = assess_oauth_engagement_records(
                account,
                credential,
                environment={"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"},
            )
        self.assertEqual(active.state, "active")

        reconnected = self._persist(
            result=self._result(scopes=(
                "instagram_business_basic",
                "instagram_business_manage_comments",
            )),
            now=first_now + timedelta(minutes=1),
        )
        with self.Session() as session:
            account = session.get(InstagramAccount, reconnected.id)
            credential = session.scalar(select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == reconnected.id
            ))
            blocked = assess_oauth_engagement_records(
                account,
                credential,
                environment={"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"},
            )
        self.assertEqual(blocked.state, "reconnect_required")

    def test_cross_user_duplicate_and_legacy_account_are_rejected_without_mutation(self):
        existing = self._persist()
        original_credential = self._credentials()[0].token_ciphertext

        with self.assertRaises(InstagramConnectionError) as cross_user:
            self._persist(user_id=self.other_owner_id)
        with self.assertRaises(InstagramConnectionError) as legacy:
            self._persist(
                discovery=self._discovery(instagram_user_id="17841438617459999"),
            )

        self.assertNotIn(existing.instagram_user_id, str(cross_user.exception))
        self.assertEqual(str(cross_user.exception), str(legacy.exception))
        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(self._accounts()[0].user_id, self.owner_id)
        self.assertEqual(self._credentials()[0].token_ciphertext, original_credential)

    def test_invalid_discovery_and_encryption_failure_leave_no_partial_rows(self):
        invalid_discoveries = (
            InstagramAccountDiscovery(instagram_user_id="", username=None),
            InstagramAccountDiscovery(instagram_user_id="not-a-numeric-instagram-id", username=None),
        )
        for discovery in invalid_discoveries:
            with self.assertRaises(InstagramConnectionError):
                self._persist(discovery=discovery)
        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

        with self.assertRaises(InstagramConnectionError):
            self._persist(keyring=object())
        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

    def test_expiry_accepts_only_bounded_positive_integer_input(self):
        now = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
        connected = self._persist(result=self._result(expires_in=3600), now=now)
        self.assertEqual(connected.token_expires_at, now + timedelta(hours=1))

        for value in (None, "3600", True, 0, -1, 10**20):
            self.assertIsNone(normalized_expires_in(value))

    def test_database_flush_failure_rolls_back_new_connection(self):
        with patch("database.credentials.Session.flush", side_effect=SQLAlchemyError("storage unavailable")):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])

    def test_database_commit_failure_preserves_existing_credential(self):
        existing = self._persist()
        with self.Session() as session:
            original_account = session.get(InstagramAccount, existing.id)
            original_credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == existing.id)
            )
            original_account_state = (
                original_account.connection_status,
                original_account.connected_at,
                original_account.disconnected_at,
                original_account.token_expires_at,
            )
            original_credential_state = (
                original_credential.token_ciphertext,
                original_credential.encryption_key_id,
                original_credential.token_status,
                original_credential.expires_at,
                original_credential.scopes,
            )
        self.token = "replacement-token-that-must-not-commit-on-failure"

        with patch.object(SessionTransaction, "commit", side_effect=SQLAlchemyError("commit unavailable")):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(len(self._accounts()), 1)
        self.assertEqual(len(self._credentials()), 1)
        with self.Session() as session:
            account = session.get(InstagramAccount, existing.id)
            credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == existing.id)
            )
            self.assertEqual(
                (
                    account.connection_status,
                    account.connected_at,
                    account.disconnected_at,
                    account.token_expires_at,
                ),
                original_account_state,
            )
            self.assertEqual(
                (
                    credential.token_ciphertext,
                    credential.encryption_key_id,
                    credential.token_status,
                    credential.expires_at,
                    credential.scopes,
                ),
                original_credential_state,
            )

    def test_unique_constraint_race_leaves_no_partial_account_or_credential(self):
        with patch.object(
            SessionTransaction,
            "commit",
            side_effect=IntegrityError("INSERT", {}, Exception("unique constraint")),
        ):
            with self.assertRaises(InstagramConnectionError):
                self._persist()

        self.assertEqual(self._accounts(), [])
        self.assertEqual(self._credentials(), [])


if __name__ == "__main__":
    unittest.main()
