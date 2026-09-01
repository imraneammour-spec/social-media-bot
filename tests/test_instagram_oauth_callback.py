import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import requests
from flask import Flask, session
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, install_auth_route_guard, register_auth_routes
from database.models import Base, InstagramAccount, InstagramCredential, User
from instagram_oauth import (
    INSTAGRAM_OAUTH_CODE_EXCHANGE_URL,
    INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL,
    INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL,
    INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
    INSTAGRAM_OAUTH_STATE_SESSION_KEY,
    OAuthExchangeError,
    OAuthDiscoveryError,
    OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED,
    OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
    OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID,
    OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID,
    OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
    InstagramOAuthTokenResult,
    complete_instagram_token_exchange,
    discover_instagram_account,
    oauth_configuration_from_environment,
)


class _ProviderResponse:
    def __init__(self, *, ok=True, payload=None, status_code=None):
        self.ok = ok
        self._payload = {} if payload is None else payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class InstagramOAuthCallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'oauth-callback.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = Flask(__name__)
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="oauth-callback-test-secret",
            legacy_instagram_user_id="legacy-instagram-id",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        with self.Session.begin() as db_session:
            user = User(email="callback-user@example.test")
            db_session.add(user)
            db_session.flush()
            self.user_id = user.id

    def tearDown(self):
        self.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _oauth_environment(self):
        return {
            "INSTAGRAM_OAUTH_APP_ID": "callback-test-app-id",
            "META_APP_SECRET": "callback-test-app-secret",
            "INSTAGRAM_OAUTH_REDIRECT_URI": "https://app.example.test/connect/instagram/callback",
        }

    def _authenticated_client(self):
        client = self.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.user_id)
            browser_session["_csrf_token"] = "callback-csrf-token"
        return client

    def _issue_state(self, client, *, user_id=None, now=None):
        with client.session_transaction() as browser_session:
            browser_session[INSTAGRAM_OAUTH_STATE_SESSION_KEY] = {
                "state": "state-for-callback-test",
                "user_id": str(user_id or self.user_id),
                "issued_at": int(time.time()) if now is None else now,
            }
        return "state-for-callback-test"

    def _assert_no_database_writes(self):
        with self.Session() as db_session:
            self.assertEqual(len(db_session.scalars(select(InstagramAccount)).all()), 0)
            self.assertEqual(len(db_session.scalars(select(InstagramCredential)).all()), 0)

    def test_valid_callback_consumes_state_before_both_exchanges_and_discards_tokens(self):
        client = self._authenticated_client()
        state = self._issue_state(client)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"
        app_secret = self._oauth_environment()["META_APP_SECRET"]

        def exchange_after_state_is_consumed(code, configuration):
            self.assertEqual(code, authorization_code)
            self.assertNotIn(INSTAGRAM_OAUTH_STATE_SESSION_KEY, session)
            return InstagramOAuthTokenResult(long_token, None, None)

        with patch.dict("os.environ", self._oauth_environment(), clear=False), patch(
            "auth.complete_instagram_token_exchange", side_effect=exchange_after_state_is_consumed
        ) as exchange, patch(
            "auth.discover_instagram_account",
            side_effect=OAuthDiscoveryError(),
        ):
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )

        self.assertEqual(response.status_code, 502)
        exchange.assert_called_once()
        rendered = response.get_data(as_text=True) + response.headers.get("Location", "")
        for sensitive_value in (authorization_code, short_token, long_token, app_secret):
            self.assertNotIn(sensitive_value, rendered)
        with client.session_transaction() as browser_session:
            self.assertNotIn(INSTAGRAM_OAUTH_STATE_SESSION_KEY, browser_session)
            self.assertNotIn(authorization_code, str(dict(browser_session)))
            self.assertNotIn(short_token, str(dict(browser_session)))
            self.assertNotIn(long_token, str(dict(browser_session)))
        self._assert_no_database_writes()

    def test_invalid_expired_replayed_cross_user_and_cross_session_state_make_zero_calls(self):
        authorization_code = "authorization-code-must-not-leak"
        cases = []

        invalid_client = self._authenticated_client()
        cases.append((invalid_client, "wrong-state"))

        expired_client = self._authenticated_client()
        self._issue_state(expired_client, now=0)
        cases.append((expired_client, "state-for-callback-test"))

        cross_user_client = self._authenticated_client()
        self._issue_state(cross_user_client, user_id=uuid.uuid4())
        cases.append((cross_user_client, "state-for-callback-test"))

        cross_session_client = self._authenticated_client()
        cases.append((cross_session_client, "state-for-callback-test"))

        for client, supplied_state in cases:
            with patch("auth.complete_instagram_token_exchange") as exchange:
                response = client.get(
                    "/connect/instagram/callback",
                    query_string={"state": supplied_state, "code": authorization_code},
                )
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(authorization_code, response.get_data(as_text=True))
            exchange.assert_not_called()

        replay_client = self._authenticated_client()
        state = self._issue_state(replay_client)
        token_result = InstagramOAuthTokenResult("long-token-must-not-leak", None, None)
        with patch.dict("os.environ", self._oauth_environment(), clear=False), patch(
            "auth.complete_instagram_token_exchange", return_value=token_result
        ) as exchange, patch(
            "auth.discover_instagram_account",
            side_effect=OAuthDiscoveryError(),
        ):
            first = replay_client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )
            replay = replay_client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )
        self.assertEqual(first.status_code, 502)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(exchange.call_count, 1)
        self._assert_no_database_writes()

    def test_provider_denial_and_missing_code_make_zero_token_calls(self):
        client = self._authenticated_client()
        state = self._issue_state(client)
        with patch("auth.complete_instagram_token_exchange") as exchange:
            denied = client.get(
                "/connect/instagram/callback",
                query_string={
                    "state": state,
                    "error": "access_denied",
                    "error_reason": "provider-reason-must-not-leak",
                    "error_description": "provider-description-must-not-leak",
                },
            )
        self.assertEqual(denied.status_code, 400)
        self.assertNotIn("provider-reason-must-not-leak", denied.get_data(as_text=True))
        self.assertNotIn("provider-description-must-not-leak", denied.get_data(as_text=True))
        exchange.assert_not_called()

        client = self._authenticated_client()
        state = self._issue_state(client)
        with patch("auth.complete_instagram_token_exchange") as exchange:
            missing_code = client.get(
                "/connect/instagram/callback", query_string={"state": state}
            )
        self.assertEqual(missing_code.status_code, 400)
        exchange.assert_not_called()
        self._assert_no_database_writes()

    def test_exchange_uses_documented_https_endpoints_form_data_parameters_and_timeouts(self):
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"
        short_response = _ProviderResponse(payload={
            "access_token": short_token,
            "permissions": (
                "instagram_business_basic,instagram_business_manage_comments,"
                "instagram_business_manage_messages"
            ),
        })
        long_response = _ProviderResponse(
            payload={
                "access_token": long_token,
                "expires_in": 3600,
            }
        )

        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post", return_value=short_response
        ) as post, patch(
            "instagram_oauth.requests.get", return_value=long_response
        ) as get:
            result = complete_instagram_token_exchange(authorization_code, configuration)

        self.assertEqual(result.long_lived_access_token, long_token)
        self.assertEqual(result.expires_in, 3600)
        self.assertEqual(result.scopes, (
            "instagram_business_basic",
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
        ))
        self.assertNotIn(long_token, repr(result))

        self.assertTrue(INSTAGRAM_OAUTH_CODE_EXCHANGE_URL.startswith("https://"))
        self.assertTrue(INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL.startswith("https://"))
        post.assert_called_once_with(
            INSTAGRAM_OAUTH_CODE_EXCHANGE_URL,
            data={
                "client_id": environment["INSTAGRAM_OAUTH_APP_ID"],
                "client_secret": environment["META_APP_SECRET"],
                "grant_type": "authorization_code",
                "redirect_uri": environment["INSTAGRAM_OAUTH_REDIRECT_URI"],
                "code": authorization_code,
            },
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        get.assert_called_once_with(
            INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": environment["META_APP_SECRET"],
                "access_token": short_token,
            },
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertNotIn(long_token, str(configuration))

    def test_short_lived_permissions_list_is_accepted(self):
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"

        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": [
                    "instagram_business_basic",
                    "instagram_business_manage_comments",
                    "instagram_business_manage_messages",
                ],
            }),
        ), patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(payload={
                "access_token": long_token,
                "expires_in": 3600,
            }),
        ):
            result = complete_instagram_token_exchange(
                "authorization-code-must-not-leak",
                configuration,
            )

        self.assertEqual(
            result.scopes,
            (
                "instagram_business_basic",
                "instagram_business_manage_comments",
                "instagram_business_manage_messages",
            ),
        )

    def test_missing_or_malformed_short_lived_permissions_fail_before_long_lived_exchange(self):
        """Using requested or long-lived scopes instead of Meta's grant evidence must fail."""
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        app_secret = environment["META_APP_SECRET"]
        invalid_permissions = (
            None,
            "",
            "instagram_business_basic,,instagram_business_manage_comments",
            "instagram_business_basic,INVALID_PERMISSION",
            [],
            ["instagram_business_basic", ""],
            ["instagram_business_basic", "INVALID_PERMISSION"],
            {"instagram_business_basic": True},
        )

        for permissions in invalid_permissions:
            with self.subTest(permissions=type(permissions).__name__), patch.dict(
                os.environ, environment, clear=False
            ), patch(
                "instagram_oauth.requests.post",
                return_value=_ProviderResponse(payload={
                    "access_token": short_token,
                    "permissions": permissions,
                }),
            ), patch("instagram_oauth.requests.get") as get:
                with self.assertRaises(OAuthExchangeError) as error:
                    complete_instagram_token_exchange(authorization_code, configuration)
            self.assertEqual(str(error.exception), "Instagram authorization could not be completed.")
            self.assertEqual(
                error.exception.diagnostic_stage,
                OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID,
            )
            self.assertNotIn(short_token, str(error.exception))
            self.assertNotIn(app_secret, str(error.exception))
            get.assert_not_called()

    def test_discovery_uses_https_bearer_auth_minimal_fields_and_sanitizes_failures(self):
        long_token = "long-token-must-not-leak"
        app_scoped_id = "17841438617458005"
        professional_id = "17841438617458006"
        token_result = InstagramOAuthTokenResult(long_token, None, None)
        with patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(payload={
                "id": app_scoped_id,
                "user_id": professional_id,
                "username": "discovered_account",
            }),
        ) as get:
            discovery = discover_instagram_account(token_result)

        self.assertEqual(discovery.instagram_user_id, professional_id)
        self.assertNotEqual(discovery.instagram_user_id, app_scoped_id)
        self.assertEqual(discovery.username, "discovered_account")
        self.assertTrue(INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL.startswith("https://"))
        get.assert_called_once_with(
            INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL,
            headers={"Authorization": f"Bearer {long_token}"},
            params={"fields": "user_id,username"},
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertNotIn(long_token, repr(discovery))

        sensitive_url = f"https://graph.instagram.com/me?access_token={long_token}"
        for failure in (
            _ProviderResponse(ok=False, payload={"error": "provider-error-must-not-leak"}),
            _ProviderResponse(payload=ValueError("provider-error-must-not-leak")),
            requests.Timeout(sensitive_url),
        ):
            with patch(
                "instagram_oauth.requests.get",
                side_effect=failure if isinstance(failure, requests.RequestException) else None,
                return_value=None if isinstance(failure, requests.RequestException) else failure,
            ):
                with self.assertRaises(OAuthDiscoveryError) as error:
                    discover_instagram_account(token_result)
            self.assertEqual(str(error.exception), "Instagram account discovery could not be completed.")
            self.assertEqual(error.exception.diagnostic_stage, OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED)
            self.assertNotIn(long_token, str(error.exception))
            self.assertNotIn(sensitive_url, str(error.exception))

        for invalid_user_id in (None, "", "not-a-numeric-instagram-id", 17841438617458006):
            with patch(
                "instagram_oauth.requests.get",
                return_value=_ProviderResponse(payload={
                    "id": app_scoped_id,
                    "user_id": invalid_user_id,
                }),
            ):
                with self.assertRaises(OAuthDiscoveryError) as error:
                    discover_instagram_account(token_result)
            self.assertEqual(str(error.exception), "Instagram account discovery could not be completed.")
            self.assertEqual(error.exception.diagnostic_stage, OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED)

    def test_successful_real_exchange_never_exposes_tokens_or_secret_to_callback_client(self):
        client = self._authenticated_client()
        state = self._issue_state(client)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"
        app_secret = self._oauth_environment()["META_APP_SECRET"]

        with patch.dict("os.environ", self._oauth_environment(), clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": "instagram_business_basic",
            }),
        ), patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(payload={"access_token": long_token}),
        ), patch(
            "auth.discover_instagram_account",
            side_effect=OAuthDiscoveryError(),
        ), patch("builtins.print") as printed:
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )

        self.assertEqual(response.status_code, 502)
        rendered = response.get_data(as_text=True) + response.headers.get("Location", "")
        for sensitive_value in (authorization_code, short_token, long_token, app_secret):
            self.assertNotIn(sensitive_value, rendered)
        self.assertEqual(
            " ".join(str(argument) for call in printed.call_args_list for argument in call.args),
            "",
        )
        with client.session_transaction() as browser_session:
            serialized_session = str(dict(browser_session))
            for sensitive_value in (authorization_code, short_token, long_token, app_secret):
                self.assertNotIn(sensitive_value, serialized_session)
        self._assert_no_database_writes()

    def test_exchange_provider_network_and_response_failures_are_generic_and_secret_free(self):
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"
        provider_body = {"error": {"message": "provider-body-must-not-leak"}}
        sensitive_request_url = (
            "https://graph.instagram.com/access_token?access_token=short-token-must-not-leak"
        )
        failing_cases = (
            ("non-2xx", _ProviderResponse(ok=False, payload=provider_body), None),
            ("malformed-json", _ProviderResponse(payload=ValueError("provider-body-must-not-leak")), None),
            ("missing-token", _ProviderResponse(payload={}), None),
            ("timeout", requests.Timeout(sensitive_request_url), None),
            ("connection", requests.ConnectionError(sensitive_request_url), None),
        )

        for _name, failure, _unused in failing_cases:
            with patch.dict(os.environ, environment, clear=False), patch(
                "instagram_oauth.requests.post", side_effect=failure
                if isinstance(failure, requests.RequestException)
                else None, return_value=None if isinstance(failure, requests.RequestException) else failure
            ), patch("instagram_oauth.requests.get") as get:
                with self.assertRaises(OAuthExchangeError) as error:
                    complete_instagram_token_exchange(authorization_code, configuration)
            message = str(error.exception)
            self.assertEqual(message, "Instagram authorization could not be completed.")
            for sensitive_value in (
                authorization_code,
                short_token,
                long_token,
                environment["META_APP_SECRET"],
                "provider-body-must-not-leak",
                sensitive_request_url,
            ):
                self.assertNotIn(sensitive_value, message)
            get.assert_not_called()

        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": "instagram_business_basic",
            }),
        ), patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(ok=False, payload=provider_body),
        ):
            with self.assertRaises(OAuthExchangeError) as error:
                complete_instagram_token_exchange(authorization_code, configuration)
        self.assertEqual(str(error.exception), "Instagram authorization could not be completed.")
        self.assertNotIn("provider-body-must-not-leak", str(error.exception))
        self.assertEqual(error.exception.diagnostic_stage, OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED)

        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": "instagram_business_basic",
            }),
        ), patch(
            "instagram_oauth.requests.get", return_value=_ProviderResponse(payload={}),
        ):
            with self.assertRaises(OAuthExchangeError) as error:
                complete_instagram_token_exchange(authorization_code, configuration)
        self.assertEqual(str(error.exception), "Instagram authorization could not be completed.")
        self.assertEqual(error.exception.diagnostic_stage, OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID)

    def test_long_exchange_non_success_captures_only_safe_meta_error_diagnostics(self):
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        provider_secret = "provider-secret-must-not-leak"
        provider_body = {
            "error": {
                "type": "OAuthException",
                "code": 190,
                "error_subcode": 460,
                "message": provider_secret,
                "access_token": short_token,
            }
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": "instagram_business_basic",
            }),
        ), patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(ok=False, status_code=400, payload=provider_body),
        ):
            with self.assertRaises(OAuthExchangeError) as error:
                complete_instagram_token_exchange(authorization_code, configuration)

        self.assertEqual(error.exception.diagnostic_stage, OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED)
        self.assertEqual(error.exception.safe_diagnostics, {
            "provider_http_status": 400,
            "provider_error_classification": "provider_error_metadata",
            "provider_error_type": "OAuthException",
            "provider_error_code": 190,
            "provider_error_subcode": 460,
        })
        diagnostics = str(error.exception.safe_diagnostics)
        for sensitive_value in (
            authorization_code,
            short_token,
            environment["META_APP_SECRET"],
            provider_secret,
        ):
            self.assertNotIn(sensitive_value, diagnostics)

    def test_long_exchange_non_json_failure_captures_status_without_provider_body(self):
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        provider_secret = "provider-error-body-must-not-leak"
        with patch.dict(os.environ, environment, clear=False), patch(
            "instagram_oauth.requests.post",
            return_value=_ProviderResponse(payload={
                "access_token": short_token,
                "permissions": "instagram_business_basic",
            }),
        ), patch(
            "instagram_oauth.requests.get",
            return_value=_ProviderResponse(
                ok=False,
                status_code=502,
                payload=ValueError(provider_secret),
            ),
        ):
            with self.assertRaises(OAuthExchangeError) as error:
                complete_instagram_token_exchange(authorization_code, configuration)

        self.assertEqual(error.exception.safe_diagnostics, {
            "provider_http_status": 502,
            "provider_error_classification": "provider_error_unparseable",
        })
        self.assertNotIn(provider_secret, str(error.exception.safe_diagnostics))

    def test_exchange_diagnostics_distinguish_short_response_permissions_and_long_response(self):
        """Collapsing token-exchange boundaries would make staging diagnosis impossible."""
        environment = self._oauth_environment()
        configuration = oauth_configuration_from_environment(environment, production=True)
        authorization_code = "authorization-code-must-not-leak"
        short_token = "short-token-must-not-leak"
        long_token = "long-token-must-not-leak"
        cases = (
            (
                "short_response",
                _ProviderResponse(ok=False, payload={"error": "provider-body-must-not-leak"}),
                None,
                OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
            ),
            (
                "permissions",
                _ProviderResponse(payload={"access_token": short_token, "permissions": ""}),
                None,
                OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID,
            ),
            (
                "long_transport",
                _ProviderResponse(payload={
                    "access_token": short_token,
                    "permissions": "instagram_business_basic",
                }),
                requests.Timeout("https://provider.example.test/private-token"),
                OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
            ),
            (
                "long_response",
                _ProviderResponse(payload={
                    "access_token": short_token,
                    "permissions": "instagram_business_basic",
                }),
                _ProviderResponse(payload={"access_token": long_token, "expires_in": "invalid"}),
                OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID,
            ),
        )

        for name, short_response, long_response, expected_stage in cases:
            with self.subTest(name=name), patch.dict(os.environ, environment, clear=False), patch(
                "instagram_oauth.requests.post", return_value=short_response
            ), patch(
                "instagram_oauth.requests.get",
                side_effect=long_response if isinstance(long_response, requests.RequestException) else None,
                return_value=None if isinstance(long_response, requests.RequestException) else long_response,
            ) as get:
                with self.assertRaises(OAuthExchangeError) as error:
                    complete_instagram_token_exchange(authorization_code, configuration)
            self.assertEqual(error.exception.diagnostic_stage, expected_stage)
            self.assertEqual(str(error.exception), "Instagram authorization could not be completed.")
            self.assertNotIn(authorization_code, str(error.exception))
            self.assertNotIn(short_token, str(error.exception))
            self.assertNotIn(long_token, str(error.exception))
            if name in {"short_response", "permissions"}:
                get.assert_not_called()

    def test_callback_exchange_failure_is_sanitized_without_logs_or_database_writes(self):
        client = self._authenticated_client()
        state = self._issue_state(client)
        authorization_code = "authorization-code-must-not-leak"
        secret = self._oauth_environment()["META_APP_SECRET"]
        sensitive_url = "https://graph.instagram.com/access_token?access_token=short-token-must-not-leak"

        with patch.dict("os.environ", self._oauth_environment(), clear=False), patch(
            "auth.complete_instagram_token_exchange",
            side_effect=OAuthExchangeError(OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID),
        ), patch.object(self.app.logger, "warning") as warning:
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )

        self.assertEqual(response.status_code, 502)
        rendered = response.get_data(as_text=True) + response.headers.get("Location", "")
        for sensitive_value in (authorization_code, secret, sensitive_url):
            self.assertNotIn(sensitive_value, rendered)
        warning.assert_called_once_with(OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID)
        self._assert_no_database_writes()

    def test_callback_logs_only_safe_long_exchange_diagnostics(self):
        client = self._authenticated_client()
        state = self._issue_state(client)
        authorization_code = "authorization-code-must-not-leak"
        safe_diagnostics = {
            "provider_http_status": 400,
            "provider_error_classification": "provider_error_metadata",
            "provider_error_type": "OAuthException",
            "provider_error_code": 190,
            "provider_error_subcode": 460,
        }
        with patch.dict("os.environ", self._oauth_environment(), clear=False), patch(
            "auth.complete_instagram_token_exchange",
            side_effect=OAuthExchangeError(
                OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
                safe_diagnostics=safe_diagnostics,
            ),
        ), patch.object(self.app.logger, "warning") as warning:
            response = client.get(
                "/connect/instagram/callback",
                query_string={"state": state, "code": authorization_code},
            )

        self.assertEqual(response.status_code, 502)
        warning.assert_called_once_with(
            OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
            extra=safe_diagnostics,
        )
        self.assertNotIn(authorization_code, str(warning.call_args))
        self.assertNotIn(self._oauth_environment()["META_APP_SECRET"], str(warning.call_args))
        self._assert_no_database_writes()


if __name__ == "__main__":
    unittest.main()
