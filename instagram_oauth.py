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
PUBLIC_BASE_URL_ENV = "PUBLIC_BASE_URL"
PUBLIC_BASE_URL = "https://postenza.app"
INSTAGRAM_OAUTH_CALLBACK_PATH = "/connect/instagram/callback"
INSTAGRAM_OAUTH_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_OAUTH_CODE_EXCHANGE_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL = "https://graph.instagram.com/access_token"
INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL = "https://graph.instagram.com/me"
INSTAGRAM_OAUTH_SCOPE = (
    "instagram_business_basic",
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
    "instagram_business_content_publish",
)
INSTAGRAM_OAUTH_STATE_SESSION_KEY = "_instagram_oauth_state"
INSTAGRAM_OAUTH_STATE_TTL_SECONDS = 600
INSTAGRAM_OAUTH_STATE_BYTES = 32
INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS = 15
INSTAGRAM_OAUTH_MAX_EXPIRES_IN_SECONDS = 60 * 60 * 24 * 366 * 10
INSTAGRAM_OAUTH_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")
OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID = "oauth_short_response_invalid"
OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID = "oauth_permissions_invalid"
OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED = "oauth_long_exchange_failed"
OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID = "oauth_long_response_invalid"
OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED = "oauth_discovery_failed"
OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED = "oauth_connection_precommit_failed"
OAUTH_CALLBACK_STAGE_STATE_MISSING = "state_missing"
OAUTH_CALLBACK_STAGE_STATE_MISMATCH_OR_SESSION_MISSING = "state_mismatch_or_session_missing"
OAUTH_CALLBACK_STAGE_PROVIDER_DENIED = "provider_denied"
OAUTH_CALLBACK_STAGE_AUTHORIZATION_CODE_MISSING = "authorization_code_missing"
OAUTH_CALLBACK_STAGE_TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
OAUTH_CALLBACK_STAGE_PERMISSIONS_FAILED = "permissions_failed"
OAUTH_CALLBACK_STAGE_CREDENTIAL_PERSISTENCE_FAILED = "credential_persistence_failed"
OAUTH_CALLBACK_STAGE_FINAL_TRANSACTION_FAILED = "final_transaction_failed"
_OAUTH_CALLBACK_DIAGNOSTIC_STAGES = frozenset((
    OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
    OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID,
    OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
    OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID,
    OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED,
    OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED,
))
_SAFE_PROVIDER_ERROR_TYPES = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_PROVIDER_ERROR_CLASSIFICATIONS = frozenset((
    "provider_error_metadata",
    "provider_error_unparseable",
))


class OAuthConfigurationError(ValueError):
    """Raised without configuration values when OAuth cannot be configured."""


class OAuthStateError(ValueError):
    """Raised without state details when authorization cannot be verified."""

    def __init__(self, diagnostic_stage: str = OAUTH_CALLBACK_STAGE_STATE_MISMATCH_OR_SESSION_MISSING):
        self.diagnostic_stage = diagnostic_stage
        super().__init__("OAuth authorization could not be verified.")


class OAuthExchangeError(ValueError):
    """Raised without provider content when an OAuth exchange fails."""

    def __init__(
        self,
        diagnostic_stage: str,
        *,
        safe_diagnostics: Mapping[str, object] | None = None,
    ):
        if diagnostic_stage not in _OAUTH_CALLBACK_DIAGNOSTIC_STAGES:
            raise ValueError("Invalid OAuth callback diagnostic stage.")
        self.diagnostic_stage = diagnostic_stage
        self.safe_diagnostics = _safe_exchange_diagnostics(safe_diagnostics)
        super().__init__("Instagram authorization could not be completed.")


class OAuthDiscoveryError(ValueError):
    """Raised without provider or account details when discovery fails."""

    diagnostic_stage = OAUTH_CALLBACK_STAGE_DISCOVERY_FAILED

    def __init__(self):
        super().__init__("Instagram account discovery could not be completed.")


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


def _validated_public_origin(value: object) -> str | None:
    """Accept only the exact canonical public origin, without forwarded-host trust."""

    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "postenza.app"
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return None
    return PUBLIC_BASE_URL


def resolve_instagram_oauth_redirect_uri(
    request_host: object,
    *,
    environ: Mapping[str, str] | None = None,
    production: bool,
) -> str:
    """Use the canonical callback only for the exact configured public host."""

    source = os.environ if environ is None else environ
    configured_origin = _validated_public_origin(source.get(PUBLIC_BASE_URL_ENV))
    if configured_origin is not None:
        configured = urlsplit(configured_origin)
        raw_host = str(request_host or "").strip()
        try:
            parsed_host = urlsplit(raw_host if "://" in raw_host else f"//{raw_host}")
            request_port = parsed_host.port
        except ValueError:
            request_port = None
            parsed_host = None
        if (
            parsed_host is not None
            and parsed_host.hostname == configured.hostname
            and request_port in (None, 443)
            and not parsed_host.username
            and not parsed_host.password
            and not parsed_host.path
            and not parsed_host.query
            and not parsed_host.fragment
        ):
            return f"{configured_origin}{INSTAGRAM_OAUTH_CALLBACK_PATH}"
    return _validated_redirect_uri(
        _required(source, INSTAGRAM_OAUTH_REDIRECT_URI_ENV), production=production
    )


def oauth_configuration_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    production: bool,
    redirect_uri: str | None = None,
) -> InstagramOAuthConfiguration:
    """Load complete OAuth configuration without exposing sensitive values."""

    source = os.environ if environ is None else environ
    app_id = _required(source, INSTAGRAM_OAUTH_APP_ID_ENV)
    # Meta requires the app secret for the next, server-only exchange phase.
    # Validate it now, but do not retain it in the authorization URL configuration.
    _required(source, META_APP_SECRET_ENV)
    redirect_uri = _validated_redirect_uri(
        redirect_uri or _required(source, INSTAGRAM_OAUTH_REDIRECT_URI_ENV),
        production=production,
    )
    return InstagramOAuthConfiguration(
        app_id=app_id,
        redirect_uri=redirect_uri,
    )


def _app_secret_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """Load the app secret only while a trusted exchange is in progress."""

    source = os.environ if environ is None else environ
    return _required(source, META_APP_SECRET_ENV)


def _exchange_error(
    diagnostic_stage: str = OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
    *,
    safe_diagnostics: Mapping[str, object] | None = None,
) -> OAuthExchangeError:
    return OAuthExchangeError(diagnostic_stage, safe_diagnostics=safe_diagnostics)


def _discovery_error() -> OAuthDiscoveryError:
    return OAuthDiscoveryError()


def _successful_response_payload(
    response: object,
    *,
    non_success_stage: str,
    invalid_response_stage: str,
) -> dict[str, object]:
    """Read one successful provider JSON object without surfacing its contents."""
    if not bool(getattr(response, "ok", False)):
        raise _exchange_error(non_success_stage)
    try:
        payload = response.json()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error(invalid_response_stage) from None
    if not isinstance(payload, dict):
        raise _exchange_error(invalid_response_stage)
    return payload


def _safe_exchange_diagnostics(value: Mapping[str, object] | None) -> dict[str, object]:
    """Keep only bounded non-secret provider diagnostics for a failed exchange."""

    if not isinstance(value, Mapping):
        return {}
    diagnostics: dict[str, object] = {}
    status = value.get("provider_http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostics["provider_http_status"] = status
    classification = value.get("provider_error_classification")
    if isinstance(classification, str) and classification in _SAFE_PROVIDER_ERROR_CLASSIFICATIONS:
        diagnostics["provider_error_classification"] = classification
    error_type = value.get("provider_error_type")
    if isinstance(error_type, str) and _SAFE_PROVIDER_ERROR_TYPES.fullmatch(error_type):
        diagnostics["provider_error_type"] = error_type
    for key in ("provider_error_code", "provider_error_subcode"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 2_147_483_647:
            diagnostics[key] = number
    return diagnostics


def _long_lived_non_success_error(response: object) -> OAuthExchangeError:
    """Classify a long-lived provider failure without retaining its payload."""

    diagnostics: dict[str, object] = {
        "provider_error_classification": "provider_error_unparseable",
    }
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostics["provider_http_status"] = status
    try:
        payload = response.json()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return _exchange_error(
            OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
            safe_diagnostics=diagnostics,
        )
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return _exchange_error(
            OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
            safe_diagnostics=diagnostics,
        )
    diagnostics["provider_error_classification"] = "provider_error_metadata"
    for source_key, diagnostic_key in (
        ("type", "provider_error_type"),
        ("code", "provider_error_code"),
        ("error_subcode", "provider_error_subcode"),
    ):
        if source_key in error:
            diagnostics[diagnostic_key] = error[source_key]
    return _exchange_error(
        OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
        safe_diagnostics=diagnostics,
    )


def _access_token_from_payload(
    payload: Mapping[str, object],
    *,
    invalid_response_stage: str,
) -> str:
    """Extract only a nonempty access token from already-read provider JSON."""

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise _exchange_error(invalid_response_stage)
    return access_token


def normalized_expires_in(value: object) -> int | None:
    """Accept only a bounded positive integer expiry from an untrusted provider."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > INSTAGRAM_OAUTH_MAX_EXPIRES_IN_SECONDS:
        return None
    return value

def _validated_granted_permissions(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Validate Meta-issued granted permissions from the short-lived OAuth response."""

    permissions = payload.get("permissions")

    if isinstance(permissions, str):
        if not permissions or len(permissions) > 8192:
            raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)
        granted = [item.strip(" \t") for item in permissions.split(",")]

    elif isinstance(permissions, list):
        if not permissions or len(permissions) > 64:
            raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)
        granted = permissions

    else:
        raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)

    if not granted or len(granted) > 64:
        raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)

    normalized: list[str] = []

    for scope in granted:
        if (
            not isinstance(scope, str)
            or not scope
            or len(scope) > 128
            or not scope.isascii()
            or not all(
                character.islower()
                or character.isdigit()
                or character == "_"
                for character in scope
            )
        ):
            raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)

        normalized.append(scope)

    if len(set(normalized)) != len(normalized):
        raise _exchange_error(OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID)

    return tuple(normalized)


def _short_lived_exchange_result(response: object) -> tuple[str, tuple[str, ...]]:
    """Return a short-lived token and only Meta-issued granted permissions."""

    payload = _successful_response_payload(
        response,
        non_success_stage=OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
        invalid_response_stage=OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
    )
    return (
        _access_token_from_payload(
            payload,
            invalid_response_stage=OAUTH_CALLBACK_STAGE_SHORT_RESPONSE_INVALID,
        ),
        _validated_granted_permissions(payload),
    )


def _long_lived_exchange_result(response: object) -> tuple[str, int | None]:
    """Return only the replacement token and optional expiry from Meta."""

    if not bool(getattr(response, "ok", False)):
        raise _long_lived_non_success_error(response)
    payload = _successful_response_payload(
        response,
        non_success_stage=OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
        invalid_response_stage=OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID,
    )
    token = _access_token_from_payload(
        payload,
        invalid_response_stage=OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID,
    )
    raw_expiry = payload.get("expires_in")
    expires_in = normalized_expires_in(raw_expiry)
    if raw_expiry is not None and expires_in is None:
        raise _exchange_error(OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID)
    return token, expires_in


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
    except OAuthConfigurationError:
        raise _exchange_error() from None
    except (requests.RequestException, AttributeError, TypeError, ValueError):
        raise _exchange_error() from None

    try:
        short_lived_token, verified_permissions = _short_lived_exchange_result(short_lived_response)
    except OAuthExchangeError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error() from None

    try:
        long_lived_response = requests.get(
            INSTAGRAM_OAUTH_LONG_LIVED_TOKEN_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": app_secret,
                "access_token": short_lived_token,
            },
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise _exchange_error(OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED) from None
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error(OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED) from None

    try:
        long_lived_token, expires_in = _long_lived_exchange_result(long_lived_response)
    except OAuthExchangeError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _exchange_error(OAUTH_CALLBACK_STAGE_LONG_RESPONSE_INVALID) from None
    return InstagramOAuthTokenResult(
        long_lived_access_token=long_lived_token,
        expires_in=expires_in,
        scopes=verified_permissions,
    )


def discover_instagram_account(token_result: InstagramOAuthTokenResult) -> InstagramAccountDiscovery:
    """Discover only the authorized account's external ID and optional username."""

    token = getattr(token_result, "long_lived_access_token", None)
    if not isinstance(token, str) or not token:
        raise _discovery_error()
    try:
        response = requests.get(
            INSTAGRAM_OAUTH_ACCOUNT_DISCOVERY_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"fields": "user_id,username"},
            timeout=INSTAGRAM_OAUTH_REQUEST_TIMEOUT_SECONDS,
        )
        if not bool(getattr(response, "ok", False)):
            raise _discovery_error()
        payload = response.json()
        if not isinstance(payload, dict):
            raise _discovery_error()
        instagram_user_id = payload.get("user_id")
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
        raise OAuthStateError(OAUTH_CALLBACK_STAGE_STATE_MISMATCH_OR_SESSION_MISSING) from None


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
        raise OAuthStateError(OAUTH_CALLBACK_STAGE_STATE_MISMATCH_OR_SESSION_MISSING)

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
        stage = (
            OAUTH_CALLBACK_STAGE_STATE_MISSING
            if not isinstance(supplied_state, str) or not supplied_state
            else OAUTH_CALLBACK_STAGE_STATE_MISMATCH_OR_SESSION_MISSING
        )
        raise OAuthStateError(stage)

    session.pop(INSTAGRAM_OAUTH_STATE_SESSION_KEY, None)
