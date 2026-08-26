import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from database.models import Base, InstagramAccount, InstagramCredential, User
from instagram_oauth import (
    INSTAGRAM_OAUTH_STATE_SESSION_KEY,
    InstagramAccountDiscovery,
    InstagramOAuthTokenResult,
    OAuthDiscoveryError,
)


class InstagramConnectionCallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'callback.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "dashboard" / "templates"),
        )
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="connection-callback-test-secret",
            legacy_instagram_user_id="17841438617459999",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)

        @self.app.get("/dashboard", endpoint="dashboard")
        def dashboard_for_test():
            return "legacy dashboard"

        with self.Session.begin() as session:
            user = User(email="connection-callback@example.test")
            session.add(user)
            session.flush()
            self.user_id = user.id

        self.authorization_code = "callback-code-must-not-leak"
        self.long_lived_token = "callback-long-lived-token-must-not-leak"
        self.external_id = "17841438617458004"
        self.environment = {
            "INSTAGRAM_OAUTH_APP_ID": "connection-callback-app-id",
            "META_APP_SECRET": "connection-callback-app-secret",
            "INSTAGRAM_OAUTH_REDIRECT_URI": "https://app.example.test/connect/instagram/callback",
            "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
            "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": Fernet.generate_key().decode("ascii"),
        }

    def tearDown(self):
        self.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _client_with_state(self):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.user_id)
            browser_session["_csrf_token"] = "connection-callback-csrf-token"
            browser_session[INSTAGRAM_OAUTH_STATE_SESSION_KEY] = {
                "state": "connection-callback-state",
                "user_id": str(self.user_id),
                "issued_at": int(time.time()),
            }
        return client

    def _token_result(self):
        return InstagramOAuthTokenResult(
            long_lived_access_token=self.long_lived_token,
            expires_in=3600,
            scopes=("instagram_business_basic",),
        )

    def _discovery(self):
        return InstagramAccountDiscovery(
            instagram_user_id=self.external_id,
            username="connected_callback_account",
        )

    def test_callback_persists_encrypted_connection_then_redirects_to_onboarding(self):
        client = self._client_with_state()
        with patch.dict("os.environ", self.environment, clear=False), patch(
            "auth.complete_instagram_token_exchange", return_value=self._token_result()
        ), patch("auth.discover_instagram_account", return_value=self._discovery()):
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": "connection-callback-state", "code": self.authorization_code},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))
        response_text = response.get_data(as_text=True) + response.headers["Location"]
        for secret in (self.authorization_code, self.long_lived_token, self.external_id):
            self.assertNotIn(secret, response_text)
        with self.Session() as session:
            accounts = session.scalars(select(InstagramAccount)).all()
            credentials = session.scalars(select(InstagramCredential)).all()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(len(credentials), 1)
            self.assertEqual(accounts[0].connection_status, "connected")
            self.assertNotEqual(credentials[0].token_ciphertext, self.long_lived_token)
            self.assertNotIn(self.long_lived_token, credentials[0].token_ciphertext)
            account_id = str(accounts[0].id)
            ciphertext = credentials[0].token_ciphertext
            key_id = credentials[0].encryption_key_id
        with client.session_transaction() as browser_session:
            serialized_session = str(dict(browser_session))
            for secret in (self.authorization_code, self.long_lived_token, self.external_id):
                self.assertNotIn(secret, serialized_session)

        onboarding = client.get("/onboarding")
        onboarding_page = onboarding.get_data(as_text=True)
        self.assertIn("Connected and ready", onboarding_page)
        self.assertNotIn('action="/connect/instagram"', onboarding_page)
        for secret in (
            self.authorization_code,
            self.long_lived_token,
            self.external_id,
            account_id,
            ciphertext,
            key_id,
        ):
            self.assertNotIn(secret, onboarding_page)

    def test_discovery_failure_returns_generic_response_and_persists_nothing(self):
        client = self._client_with_state()
        with patch.dict("os.environ", self.environment, clear=False), patch(
            "auth.complete_instagram_token_exchange", return_value=self._token_result()
        ), patch(
            "auth.discover_instagram_account",
            side_effect=OAuthDiscoveryError("Instagram account discovery could not be completed."),
        ):
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": "connection-callback-state", "code": self.authorization_code},
            )

        self.assertEqual(response.status_code, 502)
        response_text = response.get_data(as_text=True)
        for secret in (self.authorization_code, self.long_lived_token, self.external_id):
            self.assertNotIn(secret, response_text)
        with self.Session() as session:
            self.assertEqual(session.scalars(select(InstagramAccount)).all(), [])
            self.assertEqual(session.scalars(select(InstagramCredential)).all(), [])

    def test_no_account_onboarding_has_a_csrf_protected_connect_form_without_identifiers(self):
        client = self._client_with_state()

        response = client.get("/onboarding")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('method="post" action="/connect/instagram"', page)
        self.assertIn('name="csrf_token"', page)
        self.assertNotIn('disabled', page)
        self.assertNotIn("user_id", page)
        self.assertNotIn("account_id", page)
        self.assertNotIn("instagram_user_id", page)


if __name__ == "__main__":
    unittest.main()
