"""Server-only Instagram Login configuration and OAuth state helpers."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode, urlsplit

import requests
from flask import session


INSTAGRAM_OAUTH_APP_ID_ENV = "INSTAGRAM_OAUTH_APP_ID"
META_APP_SECRET_ENV = "META_APP_SECRET"
INSTAGRAM_OAUTH_REDIRECT_URI_ENV = "INSTAGRAM_OAUTH_REDIRECT_URI"
INSTAGRAM_OAUTH_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_OAUTH_CODE_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL = "https://graph.instagram.com/me"
INSTAGRAM_OAUTH_SCOPE = ("instagram_business_basic",)
INSTAGRAM_OAUTH_STATE_SESSION_KEY = "_instagram_oauth_state"
INSTAGRAM_OAUTH_STATE_TTL_SECONDS = 600
INSTAGRAM_OAUTH_STATE_BYTES = 32
INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS = 15
INSTAGRAM_OAUTH_MAX_EXPIRES_IN_SECONDS = 60 * 60 * 24 * 366 * 10
INSTAGRAM_OAUTH_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")


class OAuthConfigurationError(ValueError):
    """Raised without configuration values when OAuth cannot be configured."""


class OAuthStateError(ValueError):
    """Raised without state details when authorization cannot be verified."""


class OAuthExchangeError(ValueError):
    """Raised without provider content when an OAuth exchange fails."""


class OAuthDiscoveryError(ValueError):
    """Raised without provider or account details when discovery fails."""


@dataclass(frozen=True, repr=False)
class InstagramOAuthTokenResult:
    """Internal-only result from the server-side Meta token exchanges."""

    long_lived_access_token: str
    expires_in: int | None
    scopes: tuple[str, ...] | None


@dataclass(frozen=True, repr=False)
class InstagramAccountDiscovery:
    """Internal-only minimal account identity returned by Meta discovery."""

    instagram_user_id: str
    username: str | None


@dataclass(frozen=True)
class InstagramOAuthConfiguration:
    """Configuration needed to create an Instagram Login authorization URL."""

    app_id: str
    redirect_uri: str

    def authorization_url(self, state: str) -> str:
        """Build the fixed Instagram Login authorization URL without a secret."""

        parameters = {
            'client_id': self.app_id,
            'redirect_uri': self.redirect_uri,
            'response_type': 'code',
            'scope': ','.join(INSTAGRAM_OAUTH_SCOPE),
            'state': state,
        }
        return f"{INSTAGRAM_OAUTH_AUTHORIZE_URL}?{urlencode(parameters)}"


def _configuration_error() -> OAuthConfigurationError:
    return OAuthConfigurationError("Instagram connection configuration is unavailable.")


def _required(source: Mapping[str, str], name: str) -> str:
    value = str(source.get(name, "") or "").strip()
    if not value:
        raise _configuration_error()
    return value


def _validated_redirect_uri(value: str, *, production: bool) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise _configuration_error()
    if production and parsed.scheme != "https":
        raise _configuration_error()
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise _configuration_error()
    return value


def oauth_configuration_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    production: bool,
) -> InstagramOAuthConfiguration:
    """Load complete OAuth configuration without exposing sensitive values."""

    source = os.environ if environ is None else environ
    app_id = _required(source, INSTAGRAM_OAUTH_APP_ID_ENV)
    # Meta requires the app secret for the next, server-only exchange phase.
    # Validate it now, but do not retain it in the authorization URL configuration.
    _required(source, META_APP_SECRET_ENV)
    redirect_uri = _validated_redirect_uri(
        _required(source, INSTAGRAM_OAUTH_REDIRECT_URI_ENV), production=production
    )
    return InstagramOAuthConfiguration(
        app_id=app_id,
        redirect_uri=redirect_uri,
    )


def _app_secret_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """Load the app secret only while a trusted exchange is in progress."""

    source = os.environ if environ is None else environ
    return _required(source, META_APP_SECRET_ENV)


def _exchange_error() -> OAuthExchangeError:
    return OAuthExchangeError("Instagram authorization could not be completed.")


def _discovery_error() -> OAuthDiscoveryError:
    return OAuthDiscoveryError("Instagram account discovery could not be completed.")


def _access_token_from_response(response: object) -> str:
    """Extract only a nonempty access token without surfacing provider details."""

    if not bool(getattr(response, "ok", False)):
        raise _exchange_error()
    try:
        payload = response.json()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error() from None
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise _exchange_error()
    return access_token


def normalized_expires_in(value: object) -> int | None:
    """Accept only a bounded positive integer expiry from an untrusted provider."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > INSTAGRAM_OAUTH_MAX_EXPIRES_IN_SECONDS:
        return None
    return value


def _validated_scopes(payload: object) -> tuple[str, ...] | None:
    if not isinstance(payload, dict):
        return None
    scopes = payload.get("scopes")
    if not isinstance(scopes, list) or not scopes or len(scopes) > 64:
        return None
    normalized: list[str] = []
    for scope in scopes:
        if (
            not isinstance(scope, str)
            or not scope
            or len(scope) > 128
            or not scope.isascii()
            or not all(character.islower() or character.isdigit() or character == "_" for character in scope)
        ):
            return None
        normalized.append(scope)
    return tuple(normalized)


def _token_result_from_response(response: object) -> InstagramOAuthTokenResult:
    token = _access_token_from_response(response)
    try:
        payload = response.json()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error() from None
    if not isinstance(payload, dict):
        raise _exchange_error()
    return InstagramOAuthTokenResult(
        long_lived_access_token=token,
        expires_in=normalized_expires_in(payload.get("expires_in")),
        scopes=_validated_scopes(payload),
    )


def complete_instagram_token_exchange(
    authorization_code: object,
    configuration: InstagramOAuthConfiguration,
) -> InstagramOAuthTokenResult:
    """Perform both Meta token exchanges and return an internal-only result.

    The long-lived exchange is Meta's documented GET endpoint. Its sensitive
    parameters are therefore confined to this outbound HTTPS request and are
    never logged, returned, persisted, or included in a browser response.
    """

    if not isinstance(authorization_code, str) or not authorization_code:
        raise _exchange_error()
    try:
        app_secret = _app_secret_from_environment()
        short_lived_response = requests.post(
            INSTAGRAM_OAUTH_CODE_EXCHANGE_URL,
            data={
                "client_id": configuration.app_id,
                "client_secret": app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": configuration.redirect_uri,
                "code": authorization_code,
            },
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        short_lived_token = _access_token_from_response(short_lived_response)
        long_lived_response = requests.get(
            INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": short_lived_token,
            },
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        return _token_result_from_response(long_lived_response)
    except (OAuthConfigurationError, OAuthExchangeError):
        raise _exchange_error() from None
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        raise _exchange_error() from None


def discover_instagram_account(token_result: InstagramOAuthTokenResult) -> InstagramAccountDiscovery:
    """Discover only the authorized account's external ID and optional username."""

    token = getattr(token_result, "long_lived_access_token", None)
    if not isinstance(token, str) or not token:
        raise _discovery_error()
    try:
        response = requests.get(
            INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "id,username"},
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        if not bool(getattr(response, "ok", False)):
            raise _discovery_error()
        payload = response.json()
        if not isinstance(payload, dict):
            raise _discovery_error()
        instagram_user_id = payload.get("id")
        if (
            not isinstance(instagram_user_id, str)
            or not INSTAGRAM_OAUTH_EXTERNAL_ID_PATTERN.fullmatch(instagram_user_id)
        ):
            raise _discovery_error()
        username = payload.get("username")
        if not isinstance(username, str):
            username = None
        else:
            username = username.strip() or None
        return InstagramAccountDiscovery(
            instagram_user_id=instagram_user_id,
            username=username,
        )
    except OAuthDiscoveryError:
        raise
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        raise _discovery_error() from None


def _normalized_user_id(user_id: object) -> str:
    try:
        return str(uuid.UUID(str(user_id)))
    except (TypeError, ValueError, AttributeError):
        raise OAuthStateError("OAuth authorization could not be verified.") from None


def issue_instagram_oauth_state(user_id: object, *, now: float | None = None) -> str:
    """Issue one opaque, session-bound state value for the authenticated user."""

    normalized_user_id = _normalized_user_id(user_id)
    state = secrets.token_urlsafe(INSTAGRAM_OAUTH_STATE_BYTES)
    session[INSTAGRAM_OAUTH_STATE_SESSION_KEY] = {
        "state": state,
        "user_id": normalized_user_id,
        "issued_at": int(time.time() if now is None else now),
    }
    return state


def consume_instagram_oauth_state(
    supplied_state: object,
    user_id: object,
    *,
    now: float | None = None,
) -> None:
    """Validate and consume the session-bound OAuth state exactly once."""

    normalized_user_id = _normalized_user_id(user_id)
    record = session.get(INSTAGRAM_OAUTH_STATE_SESSION_KEY)
    current_time = int(time.time() if now is None else now)
    if not isinstance(record, dict):
        raise OAuthStateError("OAuth authorization could not be verified.")

    expected_state = record.get("state")
    expected_user_id = record.get("user_id")
    issued_at = record.get("issued_at")
    if (
        not isinstance(supplied_state, str)
        or not isinstance(expected_state, str)
        or not isinstance(expected_user_id, str)
        or not isinstance(issued_at, int)
        or issued_at > current_time
        or current_time - issued_at > INSTAGRAM_OAUTH_STATE_TTL_SECONDS
        or not hmac.compare_digest(expected_state, supplied_state)
        or not hmac.compare_digest(expected_user_id, normalized_user_id)
    ):
        raise OAuthStateError("OAuth authorization could not be verified.")

    session.pop(INSTAGRAM_OAUTH_STATE_SESSION_KEY, None)
