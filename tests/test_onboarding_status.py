import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from database.models import Base, InstagramAccount, InstagramCredential, User


class OnboardingStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'onboarding.db'}"
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
            secret_key="onboarding-test-secret",
            legacy_instagram_user_id="legacy-account-id",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)

        @self.app.get("/dashboard", endpoint="dashboard")
        def dashboard_for_test():
            return "legacy dashboard"

        @self.app.post("/api/test-mutation")
        def test_mutation():
            return {"ok": True}

    def tearDown(self):
        auth_factory = self.app.extensions["phase3a_auth"].session_factory
        if auth_factory is not None:
            auth_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_user(self, email):
        with self.Session.begin() as session:
            user = User(
                email=email,
                display_name=email.split("@", 1)[0],
                password_hash=generate_password_hash("correct horse battery staple"),
            )
            session.add(user)
            session.flush()
            return user.id

    def _create_account(
        self,
        user_id,
        *,
        instagram_user_id,
        username,
        connection_status,
        created_at=None,
        last_connection_error_code=None,
        with_credential=False,
    ):
        with self.Session.begin() as session:
            account = InstagramAccount(
                user_id=user_id,
                instagram_user_id=instagram_user_id,
                username=username,
                connection_status=connection_status,
                created_at=created_at or datetime.now(timezone.utc),
                last_connection_error_code=last_connection_error_code,
            )
            session.add(account)
            session.flush()
            if with_credential:
                session.add(
                    InstagramCredential(
                        instagram_account_id=account.id,
                        token_ciphertext="ciphertext-must-not-render",
                        encryption_key_id="KEY_2026_01_MUST_NOT_RENDER",
                        token_status="token-metadata-must-not-render",
                    )
                )
            return account.id

    def _onboarding(self, user_id, suffix=""):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(user_id)
        return client.get(f"/onboarding{suffix}")

    def test_new_user_sees_no_account_state_and_csrf_protected_connect_form(self):
        user_id = self._create_user("new@example.test")

        response = self._onboarding(user_id)

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No Instagram account connected", response.data)
        self.assertIn(b"Connect Instagram", response.data)
        self.assertIn(b'method="post" action="/connect/instagram"', response.data)
        self.assertIn(b'name="csrf_token"', response.data)
        self.assertNotIn(b"disabled", response.data)
        self.assertNotIn(b"user_id", response.data)
        self.assertNotIn(b"account_id", response.data)
        self.assertNotIn(b"instagram_user_id", response.data)

    def test_onboarding_renders_postenza_branding_and_favicon(self):
        """The account-connection page must carry the current product identity."""
        user_id = self._create_user("brand@example.test")

        page = self._onboarding(user_id).get_data(as_text=True)

        self.assertIn("Postenza", page)
        self.assertIn("postenza-logo-dark.png", page)
        self.assertIn("postenza-favicon.png", page)
        self.assertIn('alt="Postenza"', page)
        self.assertNotIn("Sociora", page)

    def test_onboarding_shows_only_owned_sanitized_accounts(self):
        owner_id = self._create_user("owner@example.test")
        other_owner_id = self._create_user("other@example.test")
        own_account_id = self._create_account(
            owner_id,
            instagram_user_id="owner-external-id-must-not-render",
            username="owned_account",
            connection_status="connected",
            with_credential=True,
        )
        self._create_account(
            other_owner_id,
            instagram_user_id="other-external-id-must-not-render",
            username="other_account_must_not_render",
            connection_status="connected",
        )

        response = self._onboarding(
            owner_id,
            suffix="?account_id=other-external-id-must-not-render",
        )
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("owned_account", page)
        self.assertIn("Connected and ready", page)
        self.assertNotIn("other_account_must_not_render", page)
        self.assertNotIn("other-external-id-must-not-render", page)
        self.assertNotIn("owner-external-id-must-not-render", page)
        self.assertNotIn(str(own_account_id), page)
        self.assertNotIn("ciphertext-must-not-render", page)
        self.assertNotIn("KEY_2026_01_MUST_NOT_RENDER", page)
        self.assertNotIn("token-metadata-must-not-render", page)

    def test_reconnect_conditions_are_generic_and_unknown_status_is_not_rendered(self):
        user_id = self._create_user("states@example.test")
        for index, status in enumerate(("disconnected", "error", "reconnect_required")):
            self._create_account(
                user_id,
                instagram_user_id=f"reconnect-{index}",
                username=f"reconnect_{index}",
                connection_status=status,
                last_connection_error_code=f"raw-error-{index}",
            )
        self._create_account(
            user_id,
            instagram_user_id="connected-with-error-id",
            username="connected_with_error",
            connection_status="connected",
            last_connection_error_code="raw-error-connected",
        )
        self._create_account(
            user_id,
            instagram_user_id="future-status-id",
            username="future_status_account",
            connection_status="future_unrecognized_status",
            last_connection_error_code="raw-error-future",
        )

        page = self._onboarding(user_id).get_data(as_text=True)

        self.assertEqual(page.count("Reconnect required"), 4)
        self.assertEqual(page.count("Reconnect Instagram"), 4)
        self.assertEqual(page.count("Disconnect from this app"), 3)
        self.assertIn("Connection setup incomplete", page)
        self.assertNotIn("raw-error-0", page)
        self.assertNotIn("raw-error-1", page)
        self.assertNotIn("raw-error-2", page)
        self.assertNotIn("raw-error-connected", page)
        self.assertNotIn("raw-error-future", page)
        self.assertNotIn("future_unrecognized_status", page)

    def test_only_configured_legacy_environment_managed_account_gets_legacy_state(self):
        legacy_owner_id = self._create_user("legacy@example.test")
        non_legacy_owner_id = self._create_user("non-legacy@example.test")
        self._create_account(
            legacy_owner_id,
            instagram_user_id="legacy-account-id",
            username="legacy_account",
            connection_status="environment_managed",
        )
        self._create_account(
            non_legacy_owner_id,
            instagram_user_id="other-environment-managed-id",
            username="other_environment_managed",
            connection_status="environment_managed",
        )

        legacy_page = self._onboarding(legacy_owner_id).get_data(as_text=True)
        non_legacy_page = self._onboarding(non_legacy_owner_id).get_data(as_text=True)

        self.assertIn("Legacy workspace managed by server configuration", legacy_page)
        self.assertNotIn("Reconnect Instagram", legacy_page)
        self.assertNotIn("Disconnect from this app", legacy_page)
        self.assertNotIn("Legacy workspace managed by server configuration", non_legacy_page)
        self.assertIn("Connection setup incomplete", non_legacy_page)

    def test_owned_accounts_render_in_creation_order(self):
        user_id = self._create_user("ordered@example.test")
        self._create_account(
            user_id,
            instagram_user_id="later-id",
            username="later_account",
            connection_status="connected",
            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        self._create_account(
            user_id,
            instagram_user_id="earlier-id",
            username="earlier_account",
            connection_status="connected",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        page = self._onboarding(user_id).get_data(as_text=True)

        self.assertLess(page.index("earlier_account"), page.index("later_account"))

    def test_legacy_owner_login_uses_onboarding_and_non_legacy_user_stays_blocked(self):
        legacy_owner_id = self._create_user("legacy-login@example.test")
        other_owner_id = self._create_user("other-login@example.test")
        self._create_account(
            legacy_owner_id,
            instagram_user_id="legacy-account-id",
            username="legacy_login_account",
            connection_status="environment_managed",
        )
        self._create_account(
            other_owner_id,
            instagram_user_id="other-login-account-id",
            username="other_login_account",
            connection_status="connected",
        )

        legacy_client = self.app.test_client()
        with legacy_client.session_transaction() as browser_session:
            browser_session["user_id"] = str(legacy_owner_id)
        self.assertTrue(legacy_client.get("/login").headers["Location"].endswith("/onboarding"))
        self.assertEqual(legacy_client.get("/dashboard").status_code, 200)

        non_legacy_client = self.app.test_client()
        with non_legacy_client.session_transaction() as browser_session:
            browser_session["user_id"] = str(other_owner_id)
        self.assertEqual(non_legacy_client.get("/dashboard").status_code, 302)
        self.assertEqual(non_legacy_client.post("/api/test-mutation").status_code, 403)


if __name__ == "__main__":
    unittest.main()
