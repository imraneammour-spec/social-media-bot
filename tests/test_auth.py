import hashlib
import hmac
import tempfile
import unittest
from pathlib import Path

from flask import Flask, session
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from auth import (
    configure_auth,
    csrf_token,
    current_user_has_legacy_workspace_access,
    get_current_user,
    install_auth_route_guard,
    register_auth_routes,
    validate_csrf_token,
    verify_meta_signature,
)
from database.models import Base, InstagramAccount, User


class AuthenticationCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'auth.db'}"
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
            secret_key="isolated-test-secret",
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

    def _create_owner(self):
        with self.Session.begin() as db_session:
            owner = User(
                email="owner@example.test",
                display_name="Owner",
                password_hash=generate_password_hash("correct horse battery staple"),
            )
            db_session.add(owner)
            db_session.flush()
            db_session.add(
                InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id="legacy-account-id",
                    username="owner_account",
                )
            )
        return owner

    def test_session_uses_random_csrf_token_and_server_side_validation(self):
        with self.app.test_request_context("/"):
            first_token = csrf_token()
            self.assertGreaterEqual(len(first_token), 32)
            self.assertTrue(validate_csrf_token(first_token))
            self.assertFalse(validate_csrf_token("wrong-token"))
            self.assertIn("_csrf_token", session)

    def test_current_user_resolves_by_uuid_and_legacy_owner_check_uses_relationship(self):
        owner = self._create_owner()

        with self.app.test_request_context("/"):
            session["user_id"] = str(owner.id)

            self.assertEqual(get_current_user().email, "owner@example.test")
            self.assertTrue(current_user_has_legacy_workspace_access())
            self.assertEqual(set(session), {"user_id"})

    def test_invalid_session_user_id_is_cleared(self):
        with self.app.test_request_context("/"):
            session["user_id"] = "not-a-uuid"

            self.assertIsNone(get_current_user())
            self.assertNotIn("user_id", session)

    def test_register_creates_non_owner_and_redirects_to_onboarding(self):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "register-csrf-token"

        response = client.post(
            "/register",
            data={
                "csrf_token": "register-csrf-token",
                "display_name": "New user",
                "email": "new@example.test",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))
        with self.Session() as db_session:
            user = db_session.scalar(select(User).where(User.email == "new@example.test"))
            self.assertIsNotNone(user)
            self.assertNotEqual(user.password_hash, "correct horse battery staple")
        self.assertEqual(client.post("/api/test-mutation").status_code, 403)
        self.assertTrue(client.get("/dashboard").headers["Location"].endswith("/onboarding"))

    def test_login_and_logout_use_csrf_and_rotate_authenticated_session(self):
        owner = self._create_owner()
        client = self.app.test_client()
        login_token = "login-csrf-token-with-32-byte-minimum"
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = login_token

        login = client.post(
            "/login",
            data={
                "csrf_token": login_token,
                "email": "owner@example.test",
                "password": "correct horse battery staple",
            },
            follow_redirects=False,
        )

        self.assertEqual(login.status_code, 302)
        self.assertTrue(login.headers["Location"].endswith("/dashboard"))
        with client.session_transaction() as browser_session:
            self.assertEqual(browser_session["user_id"], str(owner.id))
            rotated_token = browser_session["_csrf_token"]
            self.assertNotEqual(rotated_token, login_token)

        logout = client.post("/logout", data={"csrf_token": rotated_token})
        self.assertEqual(logout.status_code, 302)
        with client.session_transaction() as browser_session:
            self.assertNotIn("user_id", browser_session)

    def test_auth_forms_render_a_session_bound_csrf_token(self):
        response = self.app.test_client().get("/register")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="csrf_token"', response.data)

    def test_private_api_requires_legacy_owner_and_csrf(self):
        owner = self._create_owner()
        client = self.app.test_client()

        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(owner.id)
            browser_session["_csrf_token"] = "mutation-csrf-token"

        missing_csrf = client.post("/api/test-mutation")
        self.assertEqual(missing_csrf.status_code, 400)
        allowed = client.post(
            "/api/test-mutation",
            headers={"X-CSRF-Token": "mutation-csrf-token"},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json(), {"ok": True})

    def test_meta_signature_requires_matching_hmac_when_configured(self):
        self.app.config["META_APP_SECRET"] = "test-meta-secret"
        with self.app.test_request_context(
            "/webhook",
            method="POST",
            data=b'{"object":"instagram"}',
            headers={"X-Hub-Signature-256": "sha256=bad"},
        ):
            self.assertFalse(verify_meta_signature())

        signature = hmac.new(
            b"test-meta-secret", b'{"object":"instagram"}', hashlib.sha256
        ).hexdigest()
        with self.app.test_request_context(
            "/webhook",
            method="POST",
            data=b'{"object":"instagram"}',
            headers={"X-Hub-Signature-256": f"sha256={signature}"},
        ):
            self.assertTrue(verify_meta_signature())

    def test_database_unavailable_returns_safe_private_route_response(self):
        unavailable_app = Flask(__name__)
        configure_auth(
            unavailable_app,
            database_url=None,
            secret_key="isolated-test-secret",
            legacy_instagram_user_id="legacy-account-id",
            production=False,
        )
        register_auth_routes(unavailable_app)
        install_auth_route_guard(unavailable_app)

        @unavailable_app.get("/dashboard")
        def unavailable_dashboard():
            return "should not be reached"

        response = unavailable_app.test_client().get("/dashboard")

        self.assertEqual(response.status_code, 503)

    def test_missing_secret_key_does_not_enable_authentication(self):
        secretless_app = Flask(__name__)
        configure_auth(
            secretless_app,
            database_url=self.database_url,
            secret_key=None,
            legacy_instagram_user_id="legacy-account-id",
            production=True,
        )
        register_auth_routes(secretless_app)
        install_auth_route_guard(secretless_app)

        @secretless_app.get("/dashboard")
        def secretless_dashboard():
            return "should not be reached"

        self.assertFalse(secretless_app.extensions["phase3a_auth"].configured)
        self.assertEqual(secretless_app.test_client().get("/dashboard").status_code, 503)


if __name__ == "__main__":
    unittest.main()
