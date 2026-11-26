import unittest
from collections.abc import Mapping

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from database.credentials import (
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialNotFoundError,
    CredentialPersistenceError,
    credential_keyring_from_environment,
    decrypt_access_token,
    encrypt_access_token,
    load_decrypted_access_token,
    store_encrypted_credential,
)
from database.models import Base, InstagramAccount, InstagramCredential, User


class CredentialEncryptionTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.token = "test-instagram-token-not-for-production"
        self.old_key = Fernet.generate_key().decode("ascii")
        self.new_key = Fernet.generate_key().decode("ascii")

        with self.Session.begin() as session:
            user = User(email="credential-owner@example.test")
            session.add(user)
            session.flush()
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617457123",
            )
            session.add(account)
            session.flush()
            self.account_id = account.id

    def tearDown(self):
        self.engine.dispose()

    def _environment(self, active_key_id: str, keys: Mapping[str, str]) -> dict[str, str]:
        environment = {"INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": active_key_id}
        environment.update(
            {
                f"INSTAGRAM_CREDENTIAL_KEY_{key_id}": key_value
                for key_id, key_value in keys.items()
            }
        )
        return environment

    def _account(self, session):
        return session.scalar(
            select(InstagramAccount).where(InstagramAccount.id == self.account_id)
        )

    def _assert_secret_absent(self, error):
        rendered = f"{error!s} {error!r}"
        self.assertNotIn(self.token, rendered)
        self.assertNotIn(self.old_key, rendered)
        self.assertNotIn(self.new_key, rendered)

    def test_encrypts_and_decrypts_with_the_active_key_without_plaintext_ciphertext(self):
        keyring = credential_keyring_from_environment(
            self._environment("KEY_2026_01", {"KEY_2026_01": self.old_key})
        )

        encrypted = encrypt_access_token(self.token, keyring)

        self.assertEqual(encrypted.encryption_key_id, "KEY_2026_01")
        self.assertNotEqual(encrypted.token_ciphertext, self.token)
        self.assertNotIn(self.token, encrypted.token_ciphertext)
        self.assertEqual(
            decrypt_access_token(
                encrypted.token_ciphertext, encrypted.encryption_key_id, keyring
            ),
            self.token,
        )

    def test_store_and_load_use_ciphertext_and_one_credential_per_account(self):
        keyring = credential_keyring_from_environment(
            self._environment("KEY_2026_01", {"KEY_2026_01": self.old_key})
        )

        with self.Session.begin() as session:
            account = self._account(session)
            stored = store_encrypted_credential(session, account, self.token, keyring)
            self.assertEqual(stored.encryption_key_id, "KEY_2026_01")
            self.assertNotEqual(stored.token_ciphertext, self.token)
            self.assertNotIn(self.token, stored.token_ciphertext)

        with self.Session.begin() as session:
            account = self._account(session)
            replacement = store_encrypted_credential(
                session, account, f"{self.token}-replacement", keyring
            )
            self.assertEqual(replacement.encryption_key_id, "KEY_2026_01")

        with self.Session() as session:
            credential_rows = session.scalars(select(InstagramCredential)).all()
            self.assertEqual(len(credential_rows), 1)
            account = self._account(session)
            self.assertEqual(
                load_decrypted_access_token(session, account, keyring),
                f"{self.token}-replacement",
            )

    def test_retained_key_decrypts_existing_credential_after_active_key_rotation(self):
        old_keyring = credential_keyring_from_environment(
            self._environment("KEY_2026_01", {"KEY_2026_01": self.old_key})
        )
        rotated_keyring = credential_keyring_from_environment(
            self._environment(
                "KEY_2026_02",
                {
                    "KEY_2026_01": self.old_key,
                    "KEY_2026_02": self.new_key,
                },
            )
        )

        with self.Session.begin() as session:
            store_encrypted_credential(session, self._account(session), self.token, old_keyring)

        with self.Session() as session:
            self.assertEqual(
                load_decrypted_access_token(session, self._account(session), rotated_keyring),
                self.token,
            )

        with self.Session.begin() as session:
            rotated = store_encrypted_credential(
                session, self._account(session), f"{self.token}-rotated", rotated_keyring
            )
            self.assertEqual(rotated.encryption_key_id, "KEY_2026_02")

    def test_invalid_or_missing_key_configuration_never_leaks_token_or_key_material(self):
        with self.assertRaises(CredentialConfigurationError) as invalid_key_id:
            credential_keyring_from_environment(
                self._environment("unsafe key id", {"KEY_2026_01": self.old_key})
            )
        self._assert_secret_absent(invalid_key_id.exception)
        self.assertIsNone(invalid_key_id.exception.__cause__)

        with self.assertRaises(CredentialConfigurationError) as missing_active_key:
            credential_keyring_from_environment(
                self._environment("KEY_2026_02", {"KEY_2026_01": self.old_key})
            )
        self._assert_secret_absent(missing_active_key.exception)
        self.assertIsNone(missing_active_key.exception.__cause__)

        with self.assertRaises(CredentialConfigurationError) as malformed_fernet_key:
            credential_keyring_from_environment(
                self._environment("KEY_2026_01", {"KEY_2026_01": "not-a-fernet-key"})
            )
        self._assert_secret_absent(malformed_fernet_key.exception)
        self.assertIsNone(malformed_fernet_key.exception.__cause__)

        with self.assertRaises(CredentialConfigurationError) as unsafe_retained_key_id:
            credential_keyring_from_environment(
                {
                    "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                    "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": self.old_key,
                    "INSTAGRAM_CREDENTIAL_KEY_BAD-KEY": self.new_key,
                }
            )
        self._assert_secret_absent(unsafe_retained_key_id.exception)
        self.assertIsNone(unsafe_retained_key_id.exception.__cause__)

    def test_decryption_and_missing_credential_failures_never_leak_secrets(self):
        keyring = credential_keyring_from_environment(
            self._environment("KEY_2026_01", {"KEY_2026_01": self.old_key})
        )

        with self.assertRaises(CredentialDecryptionError) as malformed_ciphertext:
            decrypt_access_token("invalid-ciphertext", "KEY_2026_01", keyring)
        self._assert_secret_absent(malformed_ciphertext.exception)
        self.assertIsNone(malformed_ciphertext.exception.__cause__)

        with self.assertRaises(CredentialDecryptionError) as unsafe_stored_key_id:
            decrypt_access_token("invalid-ciphertext", "BAD-KEY", keyring)
        self._assert_secret_absent(unsafe_stored_key_id.exception)
        self.assertIsNone(unsafe_stored_key_id.exception.__cause__)

        with self.Session() as session:
            with self.assertRaises(CredentialNotFoundError) as missing_credential:
                load_decrypted_access_token(session, self._account(session), keyring)
        self._assert_secret_absent(missing_credential.exception)
        self.assertIsNone(missing_credential.exception.__cause__)

        with self.assertRaises(CredentialPersistenceError) as unavailable_storage:
            store_encrypted_credential(
                Session(), InstagramAccount(id=self.account_id), self.token, keyring
            )
        self._assert_secret_absent(unavailable_storage.exception)
        self.assertIsNone(unavailable_storage.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
