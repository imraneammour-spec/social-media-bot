import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.credentials import (
    CredentialConfigurationError,
    credential_keyring_from_environment,
    store_encrypted_credential,
)
from database.models import Base, InstagramAccount, InstagramCredential, User


class AccountRuntimeResolverTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.key = Fernet.generate_key().decode("ascii")
        self.keyring = credential_keyring_from_environment(
            {
                "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": self.key,
            }
        )
        self.legacy_external_id = "17841438617458901"
        self.legacy_token = "legacy-runtime-token"
        self.token_a = "oauth-runtime-token-a"
        self.token_b = "oauth-runtime-token-b"

        with self.Session.begin() as session:
            user_a = User(email="runtime-a@example.test")
            user_b = User(email="runtime-b@example.test")
            legacy_owner = User(email="runtime-legacy@example.test")
            session.add_all([user_a, user_b, legacy_owner])
            session.flush()
            account_a = InstagramAccount(
                user_id=user_a.id,
                instagram_user_id="17841438617458902",
                username="account_a",
                connection_status="connected",
                provider="instagram",
            )
            account_b = InstagramAccount(
                user_id=user_b.id,
                instagram_user_id="17841438617458903",
                username="account_b",
                connection_status="connected",
                provider="instagram",
            )
            legacy_account = InstagramAccount(
                user_id=legacy_owner.id,
                instagram_user_id=self.legacy_external_id,
                connection_status="environment_managed",
                provider="instagram",
            )
            nonlegacy_environment_account = InstagramAccount(
                user_id=user_b.id,
                instagram_user_id="17841438617458904",
                connection_status="environment_managed",
                provider="instagram",
            )
            session.add_all(
                [account_a, account_b, legacy_account, nonlegacy_environment_account]
            )
            session.flush()
            self.account_a_id = account_a.id
            self.account_b_id = account_b.id
            self.legacy_account_id = legacy_account.id
            self.nonlegacy_environment_account_id = nonlegacy_environment_account.id
            for account, token in ((account_a, self.token_a), (account_b, self.token_b)):
                credential = store_encrypted_credential(session, account, token, self.keyring)
                credential.token_status = "valid"

    def tearDown(self):
        self.engine.dispose()

    def _resolve(self, external_id, **kwargs):
        from database.account_runtime import resolve_instagram_account_runtime

        with self.Session() as session:
            return resolve_instagram_account_runtime(
                session,
                instagram_user_id=external_id,
                legacy_instagram_user_id=self.legacy_external_id,
                legacy_access_token=self.legacy_token,
                keyring=self.keyring,
                **kwargs,
            )

    def _assert_unavailable(self, external_id, **kwargs):
        from database.account_runtime import RuntimeCredentialUnavailableError

        with self.assertRaises(RuntimeCredentialUnavailableError) as raised:
            self._resolve(external_id, **kwargs)
        rendered = f"{raised.exception!s} {raised.exception!r}"
        for secret in (
            self.token_a,
            self.token_b,
            self.legacy_token,
            self.key,
            "KEY_2026_01",
            "not-a-real-ciphertext",
        ):
            self.assertNotIn(secret, rendered)
        self.assertIsNone(raised.exception.__cause__)
        return str(raised.exception)

    def _resolve_route(self, external_id, **kwargs):
        from database.account_runtime import resolve_instagram_account_route

        legacy_instagram_user_id = kwargs.pop(
            "legacy_instagram_user_id", self.legacy_external_id
        )
        with self.Session() as session:
            return resolve_instagram_account_route(
                session,
                instagram_user_id=external_id,
                legacy_instagram_user_id=legacy_instagram_user_id,
                **kwargs,
            )

    def _assert_route_unavailable(self, external_id, **kwargs):
        from database.account_runtime import RuntimeAccountRouteUnavailableError

        with self.assertRaises(RuntimeAccountRouteUnavailableError) as raised:
            self._resolve_route(external_id, **kwargs)
        self.assertIsNone(raised.exception.__cause__)
        return str(raised.exception)

    def test_token_free_route_classifies_legacy_and_connected_oauth_without_credential_access(self):
        with patch(
            "database.account_runtime.credential_keyring_from_environment",
            side_effect=AssertionError("routing must not load a keyring"),
        ), patch(
            "database.account_runtime.decrypt_access_token",
            side_effect=AssertionError("routing must not decrypt a credential"),
        ):
            legacy = self._resolve_route(self.legacy_external_id)
            oauth = self._resolve_route("17841438617458902")

        self.assertEqual(legacy.instagram_account_id, self.legacy_account_id)
        self.assertEqual(legacy.connection_mode, "legacy_environment")
        self.assertEqual(oauth.instagram_account_id, self.account_a_id)
        self.assertEqual(oauth.connection_mode, "oauth")
        rendered = repr(oauth)
        for secret in (self.token_a, self.key, "KEY_2026_01"):
            self.assertNotIn(secret, rendered)

    def test_connected_oauth_route_ignores_unusable_legacy_configuration_without_credential_access(self):
        for legacy_instagram_user_id in (None, "not-an-instagram-id"):
            with self.subTest(legacy_instagram_user_id=legacy_instagram_user_id), patch(
                "database.account_runtime.credential_keyring_from_environment",
                side_effect=AssertionError("routing must not load a keyring"),
            ), patch(
                "database.account_runtime.decrypt_access_token",
                side_effect=AssertionError("routing must not decrypt a credential"),
            ):
                route = self._resolve_route(
                    "17841438617458902",
                    legacy_instagram_user_id=legacy_instagram_user_id,
                )
                self.assertEqual(route.instagram_account_id, self.account_a_id)
                self.assertEqual(route.connection_mode, "oauth")

    def test_legacy_route_requires_usable_legacy_configuration(self):
        messages = [
            self._assert_route_unavailable(
                self.legacy_external_id,
                legacy_instagram_user_id=legacy_instagram_user_id,
            )
            for legacy_instagram_user_id in (None, "not-an-instagram-id")
        ]
        self.assertEqual(messages, [messages[0]] * len(messages))

        legacy = self._resolve_route(
            self.legacy_external_id,
            legacy_instagram_user_id=self.legacy_external_id,
        )
        self.assertEqual(legacy.instagram_account_id, self.legacy_account_id)
        self.assertEqual(legacy.connection_mode, "legacy_environment")

    def test_token_free_route_rejects_unknown_and_malformed_webhook_ids(self):
        messages = [
            self._assert_route_unavailable("17841438617458999"),
            self._assert_route_unavailable("not-an-instagram-id"),
            self._assert_route_unavailable(""),
            self._assert_route_unavailable(None),
        ]
        self.assertEqual(messages, [messages[0]] * len(messages))

    def test_token_free_route_uses_only_account_lifecycle_not_credential_metadata(self):
        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            credential.token_status = "revoked"
            credential.token_ciphertext = None
            credential.encryption_key_id = None

        route = self._resolve_route("17841438617458902")

        self.assertEqual(route.connection_mode, "oauth")
        self.assertEqual(route.instagram_account_id, self.account_a_id)

    def test_token_free_route_rejects_nonready_oauth_lifecycle_states(self):
        for status in ("disconnected", "reconnect_required", "error", "pending", "environment_managed"):
            with self.subTest(status=status):
                with self.Session.begin() as session:
                    account = session.get(InstagramAccount, self.account_a_id)
                    account.connection_status = status
                from database.account_runtime import RuntimeAccountRouteUnavailableError

                with self.Session() as session:
                    with self.assertRaises(RuntimeAccountRouteUnavailableError):
                        from database.account_runtime import resolve_instagram_account_route

                        resolve_instagram_account_route(
                            session,
                            instagram_user_id="17841438617458902",
                            legacy_instagram_user_id=self.legacy_external_id,
                        )
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a_id)
            account.connection_status = "connected"

    def test_token_free_route_rejects_errored_expired_or_wrong_provider_accounts(self):
        from database.account_runtime import RuntimeAccountRouteUnavailableError

        mutations = (
            ("last_connection_error_code", "token_invalid"),
            ("token_expires_at", datetime.now(timezone.utc) - timedelta(seconds=1)),
            ("provider", "another_provider"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                with self.Session.begin() as session:
                    account = session.get(InstagramAccount, self.account_a_id)
                    setattr(account, field, value)
                with self.Session() as session:
                    with self.assertRaises(RuntimeAccountRouteUnavailableError):
                        from database.account_runtime import resolve_instagram_account_route

                        resolve_instagram_account_route(
                            session,
                            instagram_user_id="17841438617458902",
                            legacy_instagram_user_id=self.legacy_external_id,
                        )
                with self.Session.begin() as session:
                    account = session.get(InstagramAccount, self.account_a_id)
                    setattr(account, field, None if field != "provider" else "instagram")

    def test_connected_oauth_accounts_resolve_only_their_own_decrypted_token(self):
        context_a = self._resolve("17841438617458902")
        context_b = self._resolve("17841438617458903")

        self.assertEqual(context_a.instagram_account_id, self.account_a_id)
        self.assertEqual(context_a.instagram_user_id, "17841438617458902")
        self.assertEqual(context_a.connection_mode, "oauth")
        self.assertEqual(context_a.access_token, self.token_a)
        self.assertEqual(context_b.instagram_account_id, self.account_b_id)
        self.assertEqual(context_b.access_token, self.token_b)
        self.assertNotEqual(context_a.access_token, context_b.access_token)

    def test_exact_legacy_environment_account_uses_only_the_legacy_token(self):
        context = self._resolve(self.legacy_external_id)

        self.assertEqual(context.instagram_account_id, self.legacy_account_id)
        self.assertEqual(context.connection_mode, "legacy_environment")
        self.assertEqual(context.access_token, self.legacy_token)

    def test_nonlegacy_or_wrong_status_environment_accounts_never_receive_legacy_token(self):
        self._assert_unavailable("17841438617458904")
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.legacy_account_id)
            account.connection_status = "connected"
        self._assert_unavailable(self.legacy_external_id)

    def test_unknown_and_malformed_external_ids_have_one_safe_failure(self):
        messages = [
            self._assert_unavailable("17841438617458999"),
            self._assert_unavailable("not-an-instagram-id"),
            self._assert_unavailable(""),
            self._assert_unavailable(None),
        ]
        self.assertEqual(messages, [messages[0]] * len(messages))

    def test_unusable_account_states_never_load_a_credential(self):
        for status in ("disconnected", "reconnect_required", "error", "pending", "environment_managed"):
            with self.subTest(status=status):
                with self.Session.begin() as session:
                    account = session.get(InstagramAccount, self.account_a_id)
                    account.connection_status = status
                self._assert_unavailable("17841438617458902")
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a_id)
            account.connection_status = "connected"

    def test_non_instagram_provider_never_uses_an_instagram_runtime_credential(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a_id)
            account.provider = "other_provider"
        self._assert_unavailable("17841438617458902")

    def test_missing_or_unusable_credential_data_fails_without_leaking_secrets(self):
        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            credential.token_ciphertext = None
        self._assert_unavailable("17841438617458902")

        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            credential.token_ciphertext = "not-a-real-ciphertext"
            credential.encryption_key_id = None
        self._assert_unavailable("17841438617458902")

        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            credential.token_status = "revoked"
        self._assert_unavailable("17841438617458902")

    def test_expired_oauth_credential_and_missing_credential_row_are_unusable(self):
        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            credential.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        self._assert_unavailable("17841438617458902")

        with self.Session.begin() as session:
            credential = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_b_id
                )
            )
            session.delete(credential)
        self._assert_unavailable("17841438617458903")

    def test_missing_or_invalid_key_configuration_fails_generically(self):
        from database.account_runtime import RuntimeCredentialUnavailableError

        with patch(
            "database.account_runtime.credential_keyring_from_environment",
            side_effect=CredentialConfigurationError("key material must stay secret"),
        ):
            with self.Session() as session:
                with self.assertRaises(RuntimeCredentialUnavailableError) as raised:
                    from database.account_runtime import resolve_instagram_account_runtime

                    resolve_instagram_account_runtime(
                        session,
                        instagram_user_id="17841438617458902",
                        legacy_instagram_user_id=self.legacy_external_id,
                        legacy_access_token=self.legacy_token,
                        keyring=None,
                    )
        self.assertNotIn("key material", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_runtime_context_repr_and_read_only_resolution_do_not_expose_or_mutate_secrets(self):
        with self.Session() as session:
            credential_before = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            ciphertext_before = credential_before.token_ciphertext
            key_id_before = credential_before.encryption_key_id

        context = self._resolve("17841438617458902")
        rendered = repr(context)
        for secret in (self.token_a, self.key, ciphertext_before, key_id_before):
            self.assertNotIn(str(secret), rendered)

        with self.Session() as session:
            credential_after = session.scalar(
                select(InstagramCredential).where(
                    InstagramCredential.instagram_account_id == self.account_a_id
                )
            )
            self.assertEqual(credential_after.token_ciphertext, ciphertext_before)
            self.assertEqual(credential_after.encryption_key_id, key_id_before)
            self.assertEqual(session.query(InstagramCredential).count(), 2)

if __name__ == "__main__":
    unittest.main()
