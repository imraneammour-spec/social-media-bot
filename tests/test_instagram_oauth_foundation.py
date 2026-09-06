import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from flask import Flask, session
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from database.models import Base, InstagramAccount, InstagramCredential, User
from instagram_oauth import (
    INSTAGRAM_OAUTH_SCOPE,
    INSTAGRAM_OAUTH_STATE_SESSION_KEY,
    OAuthConfigurationError,
    OAuthStateError,
    consume_instagram_oauth_state,
    issue_instagram_oauth_state,
    oauth_configuration_from_environment,
    resolve_instagram_oauth_redirect_uri,
)


class InstagramOAuthStartFoundationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'oauth.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = Flask(__name__)
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="oauth-foundation-test-secret",
            legacy_instagram_user_id="legacy-instagram-id",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        with self.Session.begin() as db_session:
            user = User(email="oauth-user@example.test")
            db_session.add(user)
            db_session.flush()
            self.user_id = user.id

    def tearDown(self):
        self.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _oauth_environment(self):
        return {
            "INSTAGRAM_OAUTH_APP_ID": "test-app-id",
            "META_APP_SECRET": "test-meta-app-secret",
            "INSTAGRAM_OAUTH_REDIRECT_URI": "https://app.example.test/connect/instagram/callback",
        }

    def _authenticated_client(self, *, csrf_token="oauth-csrf-token"):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.user_id)
            browser_session["_csrf_token"] = csrf_token
        return client

    def test_oauth_start_requires_csrf_before_it_can_issue_state(self):
        client = self._authenticated_client()

        response = client.post("/connect/instagram")

        self.assertEqual(response.status_code, 400)
        with client.session_transaction() as browser_session:
            self.assertNotIn("_instagram_oauth_state", browser_session)

    def test_oauth_configuration_requires_complete_safe_values_without_leaking_them(self):
        environment = self._oauth_environment()
        for missing_key in environment:
            faulty_environment = dict(environment)
            configured_value = faulty_environment.pop(missing_key)
            with self.assertRaises(OAuthConfigurationError) as error:
                oauth_configuration_from_environment(faulty_environment, production=True)
            rendered = f"{error.exception!s} {error.exception!r}"
            self.assertNotIn(configured_value, rendered)

        for faulty_redirect_uri in (
            "not-a-url",
            "https://app.example.test/connect/instagram/callback?next=https://evil.example",
            "http://app.example.test/connect/instagram/callback",
        ):
            faulty_environment = dict(environment)
            faulty_environment["INSTAGRAM_OAUTH_REDIRECT_URI"] = faulty_redirect_uri
            with self.assertRaises(OAuthConfigurationError) as error:
                oauth_configuration_from_environment(faulty_environment, production=True)
            self.assertNotIn(faulty_redirect_uri, str(error.exception))

    def test_oauth_configuration_requests_connection_engagement_and_publishing_scopes(self):
        configuration = oauth_configuration_from_environment(
            self._oauth_environment(), production=True
        )

        self.assertEqual(INSTAGRAM_OAUTH_SCOPE, (
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
            "instagram_business_content_publish",
        ))
        authorization_url = configuration.authorization_url("test-state")
        parameters = parse_qs(urlparse(authorization_url).query)
        self.assertEqual(parameters["scope"], [
            "instagram_business_basic,instagram_business_manage_comments,instagram_business_manage_messages,instagram_business_content_publish"
        ])
        self.assertEqual(parameters["state"], ["test-state"])
        self.assertNotIn("test-meta-app-secret", authorization_url)
        self.assertFalse(hasattr(configuration, "app_secret"))

    def test_public_origin_resolves_canonical_callback_and_other_origins_use_fallback(self):
        environment = self._oauth_environment()
        environment["PUBLIC_BASE_URL"] = "https://postenza.app"
        self.assertEqual(
            resolve_instagram_oauth_redirect_uri(
                "https://postenza.app", environ=environment, production=True
            ),
            "https://postenza.app/connect/instagram/callback",
        )
        self.assertEqual(
            resolve_instagram_oauth_redirect_uri(
                "https://social-media-bot-staging-phase5.up.railway.app",
                environ=environment,
                production=True,
            ),
            environment["INSTAGRAM_OAUTH_REDIRECT_URI"],
        )
        self.assertEqual(
            resolve_instagram_oauth_redirect_uri(
                "https://evil.example", environ=environment, production=True
            ),
            environment["INSTAGRAM_OAUTH_REDIRECT_URI"],
        )

    def test_public_origin_configuration_rejects_noncanonical_configured_base(self):
        environment = self._oauth_environment()
        environment["PUBLIC_BASE_URL"] = "https://postenza.app.evil.example"
        self.assertEqual(
            resolve_instagram_oauth_redirect_uri(
                "https://postenza.app", environ=environment, production=True
            ),
            environment["INSTAGRAM_OAUTH_REDIRECT_URI"],
        )

    def test_state_is_random_and_contains_no_meta_credentials(self):
        with self.app.test_request_context("/"):
            first_state = issue_instagram_oauth_state(self.user_id, now=1000)
            second_state = issue_instagram_oauth_state(self.user_id, now=1001)

            self.assertNotEqual(first_state, second_state)
            self.assertGreaterEqual(len(first_state), 32)
            stored_state = session[INSTAGRAM_OAUTH_STATE_SESSION_KEY]
            self.assertEqual(set(stored_state), {"state", "user_id", "issued_at"})
            self.assertNotIn("token", str(stored_state).lower())
            self.assertNotIn("secret", str(stored_state).lower())

    def test_state_is_bound_to_user_session_and_expiration(self):
        other_user_id = uuid.uuid4()
        with self.app.test_request_context("/"):
            state = issue_instagram_oauth_state(self.user_id, now=1000)
            with self.assertRaises(OAuthStateError):
                consume_instagram_oauth_state(state, other_user_id, now=1001)
            with self.assertRaises(OAuthStateError):
                consume_instagram_oauth_state("wrong-state", self.user_id, now=1001)
            with self.assertRaises(OAuthStateError):
                consume_instagram_oauth_state(state, self.user_id, now=1701)

        with self.app.test_request_context("/"):
            with self.assertRaises(OAuthStateError):
                consume_instagram_oauth_state(state, self.user_id, now=1001)

    def test_successful_state_validation_consumes_state_and_replay_fails(self):
        with self.app.test_request_context("/"):
            state = issue_instagram_oauth_state(self.user_id, now=1000)
            consume_instagram_oauth_state(state, self.user_id, now=1001)

            self.assertNotIn(INSTAGRAM_OAUTH_STATE_SESSION_KEY, session)
            with self.assertRaises(OAuthStateError) as replay_error:
                consume_instagram_oauth_state(state, self.user_id, now=1002)
            self.assertEqual(str(replay_error.exception), "OAuth authorization could not be verified.")

    def test_malformed_or_missing_state_fails_generically(self):
        with self.app.test_request_context("/"):
            for supplied_state in (None, "", "malformed"):
                with self.assertRaises(OAuthStateError) as error:
                    consume_instagram_oauth_state(supplied_state, self.user_id, now=1000)
                self.assertEqual(str(error.exception), "OAuth authorization could not be verified.")

            session[INSTAGRAM_OAUTH_STATE_SESSION_KEY] = "not-a-state-record"
            with self.assertRaises(OAuthStateError) as error:
                consume_instagram_oauth_state("malformed", self.user_id, now=1000)
            self.assertEqual(str(error.exception), "OAuth authorization could not be verified.")

    def test_valid_csrf_start_redirects_without_account_or_credential_writes(self):
        client = self._authenticated_client()
        with patch.dict("os.environ", self._oauth_environment(), clear=False):
            response = client.post(
                "/connect/instagram",
                data={"csrf_token": "oauth-csrf-token"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("https://www.instagram.com/oauth/authorize", response.headers["Location"])
        with self.Session() as db_session:
            self.assertEqual(len(db_session.scalars(select(InstagramAccount)).all()), 0)
            self.assertEqual(len(db_session.scalars(select(InstagramCredential)).all()), 0)

    def test_start_configuration_failure_is_generic_and_issues_no_state(self):
        client = self._authenticated_client()
        environment = self._oauth_environment()
        app_id = environment.pop("INSTAGRAM_OAUTH_APP_ID")

        with patch.dict("os.environ", environment, clear=True):
            response = client.post("/connect/instagram", data={"csrf_token": "oauth-csrf-token"})

        self.assertEqual(response.status_code, 503)
        self.assertNotIn(app_id, response.get_data(as_text=True))
        self.assertNotIn("test-meta-app-secret", response.get_data(as_text=True))
        with client.session_transaction() as browser_session:
            self.assertNotIn(INSTAGRAM_OAUTH_STATE_SESSION_KEY, browser_session)

    def test_legacy_owner_cannot_start_oauth_or_receive_state(self):
        with self.Session.begin() as db_session:
            db_session.add(
                InstagramAccount(
                    user_id=self.user_id,
                    instagram_user_id="legacy-instagram-id",
                    connection_status="environment_managed",
                )
            )
        client = self._authenticated_client()
        with patch.dict("os.environ", self._oauth_environment(), clear=False):
            response = client.post(
                "/connect/instagram", data={"csrf_token": "oauth-csrf-token"}
            )

        self.assertEqual(response.status_code, 403)
        with client.session_transaction() as browser_session:
            self.assertNotIn(INSTAGRAM_OAUTH_STATE_SESSION_KEY, browser_session)


if __name__ == "__main__":
    unittest.main()
