import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

import auth
from auth import (
    configure_auth,
    install_auth_route_guard,
    issue_email_verification_token,
    register_auth_routes,
)
from database.models import Base, User, UserOnboardingProfile


class EmailVerificationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'verify.db'}"
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
            secret_key="email-verification-test-secret",
            legacy_instagram_user_id="legacy-account-id",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)

    def tearDown(self):
        auth_factory = self.app.extensions["phase3a_auth"].session_factory
        if auth_factory is not None:
            auth_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_user(self, email, *, profile=False, completed=False, verified=False):
        with self.Session.begin() as session:
            user = User(
                email=email,
                display_name=email.split("@", 1)[0],
                password_hash=generate_password_hash("correct horse battery staple"),
                email_verified_at=(
                    datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
                    if verified
                    else None
                ),
            )
            session.add(user)
            session.flush()
            if profile:
                session.add(
                    UserOnboardingProfile(
                        user_id=user.id,
                        use_case="business_account" if completed else None,
                        main_goal="save_time" if completed else None,
                        onboarding_source="google" if completed else None,
                        selected_plan="free" if completed else None,
                        completed_at=(
                            datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
                            if completed
                            else None
                        ),
                    )
                )
            return user.id

    def _client_for(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(user_id)
            browser_session["_csrf_token"] = "verification-csrf-token-32-bytes-ok"
        return client

    def _issue_token(self, user_id):
        with self.Session() as session:
            user = session.get(User, user_id)
        with self.app.test_request_context("/"):
            return issue_email_verification_token(user)

    def _stored_user(self, user_id):
        with self.Session() as session:
            return session.get(User, user_id)

    def test_registration_issues_token_and_logs_link_without_smtp(self):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "register-csrf-token"

        with self.assertLogs(self.app.logger, level="WARNING") as logs:
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
        self.assertIn("SMTP is not configured", "\n".join(logs.output))
        with self.Session() as session:
            user = session.scalar(select(User).where(User.email == "new@example.test"))
            self.assertIsNotNone(user)
            self.assertIsNone(user.email_verified_at)
            preferences = user.notification_preferences or {}
            self.assertIsInstance(preferences.get("email_verification_nonce"), str)

    def test_unverified_completed_user_blocked_from_instagram_connect(self):
        user_id = self._create_user(
            "unverified@example.test", profile=True, completed=True
        )
        client = self._client_for(user_id)

        page = client.get("/onboarding").get_data(as_text=True)
        self.assertIn("Verify your email to Connect Instagram", page)
        self.assertIn('action="/auth/verify-email/resend"', page)
        self.assertNotIn('action="/connect/instagram"', page)

        response = client.post(
            "/connect/instagram",
            data={"csrf_token": "verification-csrf-token-32-bytes-ok"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Verify your email", response.get_data(as_text=True))

    def test_verified_completed_user_passes_connect_gate(self):
        user_id = self._create_user(
            "verified@example.test", profile=True, completed=True, verified=True
        )
        client = self._client_for(user_id)
        environment = {
            "INSTAGRAM_OAUTH_APP_ID": "verification-test-app-id",
            "META_APP_SECRET": "verification-test-app-secret",
            "INSTAGRAM_OAUTH_REDIRECT_URI": "https://app.example.test/connect/instagram/callback",
        }

        with patch.dict("os.environ", environment, clear=False):
            response = client.post(
                "/connect/instagram",
                data={"csrf_token": "verification-csrf-token-32-bytes-ok"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            "https://www.instagram.com/oauth/authorize",
            response.headers["Location"],
        )

    def test_valid_token_marks_email_verified_and_replay_is_idempotent(self):
        user_id = self._create_user("verify-me@example.test", profile=True)
        token = self._issue_token(user_id)
        client = self._client_for(user_id)

        response = client.get(
            "/auth/verify-email", query_string={"token": token}, follow_redirects=False
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))
        stored = self._stored_user(user_id)
        self.assertIsNotNone(stored.email_verified_at)
        self.assertNotIn(
            "email_verification_nonce", stored.notification_preferences or {}
        )

        # The token is single-use: its nonce was consumed, so a replay
        # cannot verify anything again. The user is already verified, so
        # the replay simply sends them onward instead of erroring.
        replay = client.get(
            "/auth/verify-email", query_string={"token": token}, follow_redirects=False
        )
        self.assertEqual(replay.status_code, 302)
        self.assertTrue(replay.headers["Location"].endswith("/onboarding"))

    def test_resend_rotates_token_and_invalidates_previous_link(self):
        user_id = self._create_user("resend@example.test", profile=True)
        old_token = self._issue_token(user_id)
        client = self._client_for(user_id)

        response = client.post(
            "/auth/verify-email/resend",
            data={"csrf_token": "verification-csrf-token-32-bytes-ok"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))
        stale = client.get(
            "/auth/verify-email",
            query_string={"token": old_token},
            follow_redirects=False,
        )
        self.assertEqual(stale.status_code, 400)
        self.assertIn("already been used", stale.get_data(as_text=True))

    def test_expired_token_is_rejected(self):
        user_id = self._create_user("expired@example.test", profile=True)
        token = self._issue_token(user_id)
        client = self._client_for(user_id)

        with patch.object(auth, "EMAIL_VERIFICATION_TOKEN_MAX_AGE_SECONDS", -1):
            response = client.get(
                "/auth/verify-email",
                query_string={"token": token},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True))
        self.assertIsNone(self._stored_user(user_id).email_verified_at)

    def test_tampered_token_is_rejected(self):
        user_id = self._create_user("tampered@example.test", profile=True)
        token = self._issue_token(user_id)
        client = self._client_for(user_id)

        response = client.get(
            "/auth/verify-email",
            query_string={"token": token + "tampered"},
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("invalid", response.get_data(as_text=True))
        self.assertIsNone(self._stored_user(user_id).email_verified_at)

    def test_resend_is_rate_limited(self):
        user_id = self._create_user("limited@example.test", profile=True)
        client = self._client_for(user_id)

        statuses = [
            client.post(
                "/auth/verify-email/resend",
                data={"csrf_token": "verification-csrf-token-32-bytes-ok"},
                follow_redirects=False,
            ).status_code
            for _ in range(4)
        ]

        self.assertEqual(statuses, [303, 303, 303, 429])

    def test_legacy_user_without_profile_is_not_blocked(self):
        user_id = self._create_user("legacy@example.test")
        client = self._client_for(user_id)

        page = client.get("/onboarding").get_data(as_text=True)
        self.assertIn('method="post" action="/connect/instagram"', page)
        self.assertNotIn("Verify your email", page)

        # No OAuth configuration is set, so a passed gate fails later with
        # 503 instead of the 403 an unverified new-flow user receives.
        response = client.post(
            "/connect/instagram",
            data={"csrf_token": "verification-csrf-token-32-bytes-ok"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
