"""Server-only Instagram Login configuration and OAuth state helpers."""

from __future__ import annotations

import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode, urlsplit

from flask import session


INSTAGRAM_OAUTH_APP_ID_ENV = "INSTAGRAM_OAUTH_APP_ID"
META_APP_SECRET_ENV = "META_APP_SECRET"
INSTAGRAM_OAUTH_REDIRECT_URI_ENV = "INSTAGRAM_OAUTH_REDIRECT_URI"
INSTAGRAM_OAUTH_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_OAUTH_SCOPE = ("instagram_business_basic",)
INSTAGRAM_OAUTH_STATE_SESSION_KEY = "_instagram_oauth_state"
INSTAGRAM_OAUTH_STATE_TTL_SECONDS = 600
INSTAGRAM_OAUTH_STATE_BYTES = 32


class OAuthConfigurationError(ValueError):
    """Raised without configuration values when OAuth cannot be configured."""


class OAuthStateError(ValueError):
    """Raised without state details when authorization cannot be verified."""


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
