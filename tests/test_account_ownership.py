import tempfile
import unittest
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.credentials import (
    CredentialNotFoundError,
    credential_keyring_from_environment,
    load_decrypted_access_token_for_user,
    store_encrypted_credential_for_user,
)
from database.models import Base, InstagramAccount, InstagramCredential, User
from database.ownership import (
    resolve_owned_instagram_account,
    resolve_owned_instagram_account_by_external_id,
)


class AccountOwnershipIsolationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.temp_dir.name) / 'ownership.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.owner_token = "owner-test-instagram-token"
        self.other_token = "other-owner-test-instagram-token"
        self.key = Fernet.generate_key().decode("ascii")
        self.keyring = credential_keyring_from_environment(
            {
                "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": self.key,
            }
        )

        with self.Session.begin() as session:
            owner = User(email="owner@example.test")
            other_owner = User(email="other-owner@example.test")
            session.add_all([owner, other_owner])
            session.flush()
            owner_account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617457001",
            )
            other_account = InstagramAccount(
                user_id=other_owner.id,
                instagram_user_id="17841438617457002",
            )
            session.add_all([owner_account, other_account])
            session.flush()
            self.owner_id = owner.id
            self.other_owner_id = other_owner.id
            self.owner_account_id = owner_account.id
            self.other_account_id = other_account.id

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _credential_count(self, session, account_id):
        return len(
            session.scalars(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == account_id
                )
            ).all()
        )

    def test_owner_resolves_only_own_account(self):
        with self.Session() as session:
            resolved = resolve_owned_instagram_account(
                session, user_id=self.owner_id, account_id=self.owner_account_id
            )
            legacy_resolved = resolve_owned_instagram_account_by_external_id(
                session,
                user_id=self.owner_id,
                instagram_user_id="17841438617457001",
            )

        self.assertEqual(resolved.id, self.owner_account_id)
        self.assertEqual(legacy_resolved.id, self.owner_account_id)

    def test_non_owned_nonexistent_and_invalid_identifiers_have_one_safe_result(self):
        with self.Session() as session:
            wrong_owner = resolve_owned_instagram_account(
                session, user_id=self.owner_id, account_id=self.other_account_id
            )
            nonexistent = resolve_owned_instagram_account(
                session, user_id=self.owner_id, account_id=uuid.uuid4()
            )
            malformed = resolve_owned_instagram_account(
                session, user_id=self.owner_id, account_id="not-a-uuid"
            )
            unusable = resolve_owned_instagram_account(
                session, user_id=self.owner_id, account_id=""
            )

        self.assertIsNone(wrong_owner)
        self.assertIsNone(nonexistent)
        self.assertIsNone(malformed)
        self.assertIsNone(unusable)

    def test_user_scoped_credential_access_requires_account_ownership(self):
        with self.Session.begin() as session:
            stored = store_encrypted_credential_for_user(
                session,
                user_id=self.owner_id,
                account_id=self.owner_account_id,
                access_token=self.owner_token,
                keyring=self.keyring,
            )
            self.assertEqual(stored.instagram_account_id, self.owner_account_id)

        with self.Session() as session:
            self.assertEqual(
                load_decrypted_access_token_for_user(
                    session,
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    keyring=self.keyring,
                ),
                self.owner_token,
            )

    def test_user_cannot_load_or_replace_another_users_credential(self):
        with self.Session.begin() as session:
            other_credential = store_encrypted_credential_for_user(
                session,
                user_id=self.other_owner_id,
                account_id=self.other_account_id,
                access_token=self.other_token,
                keyring=self.keyring,
            )
            original_ciphertext = other_credential.token_ciphertext

        errors = []
        with self.Session.begin() as session:
            for account_id in (self.other_account_id, uuid.uuid4(), "not-a-uuid"):
                with self.assertRaises(CredentialNotFoundError) as denied:
                    store_encrypted_credential_for_user(
                        session,
                        user_id=self.owner_id,
                        account_id=account_id,
                        access_token=self.owner_token,
                        keyring=self.keyring,
                    )
                errors.append(str(denied.exception))

        with self.Session() as session:
            for account_id in (self.other_account_id, uuid.uuid4(), "not-a-uuid"):
                with self.assertRaises(CredentialNotFoundError) as denied:
                    load_decrypted_access_token_for_user(
                        session,
                        user_id=self.owner_id,
                        account_id=account_id,
                        keyring=self.keyring,
                    )
                errors.append(str(denied.exception))

            other_credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.other_account_id
                )
            )
            self.assertEqual(other_credential.token_ciphertext, original_ciphertext)
            self.assertEqual(self._credential_count(session, self.other_account_id), 1)

        self.assertEqual(errors, ["Credential is unavailable."] * 6)

    def test_owner_replacement_keeps_one_credential_per_account(self):
        with self.Session.begin() as session:
            store_encrypted_credential_for_user(
                session,
                user_id=self.owner_id,
                account_id=self.owner_account_id,
                access_token=self.owner_token,
                keyring=self.keyring,
            )
            store_encrypted_credential_for_user(
                session,
                user_id=self.owner_id,
                account_id=self.owner_account_id,
                access_token=f"{self.owner_token}-replacement",
                keyring=self.keyring,
            )

        with self.Session() as session:
            self.assertEqual(self._credential_count(session, self.owner_account_id), 1)
            self.assertEqual(
                load_decrypted_access_token_for_user(
                    session,
                    user_id=self.owner_id,
                    account_id=self.owner_account_id,
                    keyring=self.keyring,
                ),
                f"{self.owner_token}-replacement",
            )


if __name__ == "__main__":
    unittest.main()
