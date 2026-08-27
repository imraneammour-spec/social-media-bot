import re
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import (
    ACCOUNT_ACTION_REFERENCE_TTL_SECONDS,
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
    resolve_owned_account_action_reference,
)
from database.credentials import (
    CredentialDecryptionError,
    credential_keyring_from_environment,
    load_decrypted_access_token,
    store_encrypted_credential,
)
from database.instagram_connection import (
    InstagramConnectionError,
    disconnect_owned_instagram_connection,
)
from database.models import Base, InstagramAccount, InstagramCredential, User


class InstagramDisconnectTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'disconnect.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.keyring = credential_keyring_from_environment(
            {
                "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
                "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": Fernet.generate_key().decode("ascii"),
            }
        )
        self.app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "dashboard" / "templates"),
        )
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="disconnect-action-reference-test-secret",
            legacy_instagram_user_id="17841438617459999",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        with self.Session.begin() as session:
            owner = User(email="disconnect-owner@example.test")
            other_owner = User(email="disconnect-other@example.test")
            session.add_all([owner, other_owner])
            session.flush()
            self.owner_id, self.other_owner_id = owner.id, other_owner.id
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458011",
                username="disconnect_account",
                connection_status="connected",
                connected_at=datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc),
            )
            self.legacy_account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617459999",
                username="legacy_account",
                connection_status="environment_managed",
            )
            session.add_all([self.account, self.legacy_account])
            session.flush()
            self.account_id, self.legacy_account_id = self.account.id, self.legacy_account.id
            credential = store_encrypted_credential(
                session, self.account, "disconnect-token-must-not-survive", self.keyring
            )
            credential.token_status = "valid"
            credential.issued_at = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
            credential.last_validated_at = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
            self.original_issued_at, self.original_validated_at = credential.issued_at, credential.last_validated_at

    def tearDown(self):
        self.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _disconnect(self, *, user_id=None, account_id=None, now=None):
        return disconnect_owned_instagram_connection(
            self.Session,
            user_id=user_id or self.owner_id,
            account_id=account_id or self.account_id,
            legacy_instagram_user_id="17841438617459999",
            now=now or datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
        )

    def test_disconnect_erases_usable_credential_and_preserves_history_idempotently(self):
        first = self._disconnect()
        second = self._disconnect(now=datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc))

        self.assertEqual(first.id, second.id)
        with self.Session() as session:
            account = session.get(InstagramAccount, self.account_id)
            credential = session.scalar(select(InstagramCredential).where(InstagramCredential.instagram_account_id == self.account_id))
            self.assertEqual(account.connection_status, "disconnected")
            self.assertEqual(account.disconnected_at.replace(tzinfo=timezone.utc), datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))
            self.assertIsNone(account.token_expires_at)
            self.assertIsNone(account.last_connection_error_code)
            self.assertEqual(credential.token_status, "disconnected")
            self.assertIsNone(credential.token_ciphertext)
            self.assertIsNone(credential.encryption_key_id)
            self.assertIsNone(credential.scopes)
            self.assertIsNone(credential.expires_at)
            self.assertIsNone(credential.revoked_at)
            self.assertIsNone(credential.last_error_code)
            self.assertEqual(credential.issued_at.replace(tzinfo=timezone.utc), self.original_issued_at)
            self.assertEqual(credential.last_validated_at.replace(tzinfo=timezone.utc), self.original_validated_at)
            with self.assertRaises(CredentialDecryptionError):
                load_decrypted_access_token(session, account, self.keyring)

    def test_cross_user_and_legacy_disconnect_attempts_have_one_safe_failure_and_no_mutation(self):
        with self.assertRaises(InstagramConnectionError) as cross_user:
            self._disconnect(user_id=self.other_owner_id)
        with self.assertRaises(InstagramConnectionError) as legacy:
            self._disconnect(account_id=self.legacy_account_id)
        self.assertEqual(str(cross_user.exception), str(legacy.exception))
        with self.Session() as session:
            account = session.get(InstagramAccount, self.account_id)
            legacy_account = session.get(InstagramAccount, self.legacy_account_id)
            credential = session.scalar(select(InstagramCredential).where(InstagramCredential.instagram_account_id == self.account_id))
            self.assertEqual(account.connection_status, "connected")
            self.assertEqual(legacy_account.connection_status, "environment_managed")
            self.assertIsNotNone(credential.token_ciphertext)

    def test_opaque_action_references_are_tenant_purpose_bound_and_expire(self):
        now = 1_000_000
        reference = action_reference_for_account(
            self.owner_id, self.account_id, "disconnect", secret_key=self.app.secret_key, now=now
        )
        self.assertNotIn(str(self.account_id), reference)
        self.assertNotIn("17841438617458011", reference)
        self.assertRegex(reference, r"^v1\.\d+\.[A-Za-z0-9_-]+$")
        with self.Session() as session:
            self.assertEqual(
                resolve_owned_account_action_reference(
                    session, user_id=self.owner_id, reference=reference, purpose="disconnect",
                    secret_key=self.app.secret_key, now=now + 1,
                ).id,
                self.account_id,
            )
            for user_id, purpose, value, reference_now in (
                (self.other_owner_id, "disconnect", reference, now + 1),
                (self.owner_id, "connect", reference, now + 1),
                (self.owner_id, "disconnect", "malformed", now + 1),
                (self.owner_id, "disconnect", reference, now + ACCOUNT_ACTION_REFERENCE_TTL_SECONDS + 1),
            ):
                self.assertIsNone(resolve_owned_account_action_reference(
                    session, user_id=user_id, reference=value, purpose=purpose,
                    secret_key=self.app.secret_key, now=reference_now,
                ))

    def test_onboarding_disconnect_action_is_csrf_protected_and_exposes_no_identifiers(self):
        client = self.app.test_client()
        csrf_value = "disconnect-csrf-token-with-security-grade-length"
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.owner_id)
            browser_session["_csrf_token"] = csrf_value

        page = client.get("/onboarding").get_data(as_text=True)
        action_match = re.search(r'name="account_action" value="([^"]+)"', page)
        self.assertIn("Disconnect from this app", page)
        self.assertIn('method="post" action="/connect/instagram/disconnect"', page)
        self.assertIsNotNone(action_match)
        reference = action_match.group(1)
        for secret in (str(self.account_id), "17841438617458011", "disconnect-token-must-not-survive"):
            self.assertNotIn(secret, page)

        missing_csrf = client.post("/connect/instagram/disconnect", data={"account_action": reference})
        self.assertEqual(missing_csrf.status_code, 400)
        disconnected = client.post(
            "/connect/instagram/disconnect",
            data={"csrf_token": csrf_value, "account_action": reference},
        )
        self.assertEqual(disconnected.status_code, 302)
        self.assertTrue(disconnected.headers["Location"].endswith("/onboarding"))

    def test_invalid_expired_and_cross_user_disconnect_actions_have_one_safe_response(self):
        owner_client = self.app.test_client()
        other_client = self.app.test_client()
        csrf_value = "disconnect-csrf-token-with-security-grade-length"
        with owner_client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.owner_id)
            browser_session["_csrf_token"] = csrf_value
        with other_client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.other_owner_id)
            browser_session["_csrf_token"] = csrf_value

        page = owner_client.get("/onboarding").get_data(as_text=True)
        reference = re.search(r'name="account_action" value="([^"]+)"', page).group(1)
        expired = action_reference_for_account(
            self.owner_id,
            self.account_id,
            "disconnect",
            secret_key=self.app.secret_key,
            now=1,
        )
        attempts = (
            owner_client.post(
                "/connect/instagram/disconnect",
                data={"csrf_token": csrf_value, "account_action": "malformed"},
            ),
            owner_client.post(
                "/connect/instagram/disconnect",
                data={"csrf_token": csrf_value, "account_action": expired},
            ),
            other_client.post(
                "/connect/instagram/disconnect",
                data={"csrf_token": csrf_value, "account_action": reference},
            ),
        )
        for response in attempts:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_data(as_text=True), "Instagram connection could not be completed.")
        with self.Session() as session:
            account = session.get(InstagramAccount, self.account_id)
            credential = session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == self.account_id)
            )
            self.assertEqual(account.connection_status, "connected")
            self.assertIsNotNone(credential.token_ciphertext)


if __name__ == "__main__":
    unittest.main()
