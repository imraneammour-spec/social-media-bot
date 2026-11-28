"""Authentication primitives for the additive Phase 3A Flask flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import smtplib
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps
from typing import Callable, TypeVar
from urllib.parse import urlsplit

from flask import Response, current_app, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from dashboard.i18n import LOCALE_META, LOCALE_SESSION_KEY, locale_context, normalize_locale, session_locale, translate

from database.credentials import CredentialConfigurationError, credential_keyring_from_environment
from database.instagram_connection import (
    InstagramConnectionError,
    complete_discovered_instagram_connection,
    disconnect_owned_instagram_connection,
)
from database.models import InstagramAccount, User, UserOnboardingProfile
from database.onboarding import (
    MAIN_GOAL_VALUES,
    ONBOARDING_SOURCE_VALUES,
    PLAN_VALUES,
    USE_CASE_VALUES,
    OnboardingValidationError,
    apply_onboarding_step,
    expected_onboarding_step,
    onboarding_profile_for_user,
    user_onboarding_is_complete,
)
from database.ownership import (
    list_owned_instagram_accounts,
    resolve_owned_instagram_account_by_external_id,
)
from database.runtime import create_session_factory
from instagram_oauth import (
    OAuthConfigurationError,
    OAuthDiscoveryError,
    OAuthExchangeError,
    OAuthStateError,
    OAUTH_CALLBACK_STAGE_AUTHORIZATION_CODE_MISSING,
    OAUTH_CALLBACK_STAGE_CREDENTIAL_PERSISTENCE_FAILED,
    OAUTH_CALLBACK_STAGE_PROVIDER_DENIED,
    OAUTH_CALLBACK_STAGE_PERMISSIONS_FAILED,
    OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID,
    OAUTH_CALLBACK_STAGE_FINAL_TRANSACTION_FAILED,
    OAUTH_CALLBACK_STAGE_TOKEN_EXCHANGE_FAILED,
    PUBLIC_BASE_URL,
    OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED,
    OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
    complete_instagram_token_exchange,
    consume_instagram_oauth_state,
    discover_instagram_account,
    issue_instagram_oauth_state,
    oauth_configuration_from_environment,
    resolve_instagram_oauth_redirect_uri,
)


CSRF_SESSION_KEY = "_csrf_token"
USER_SESSION_KEY = "user_id"
CSRF_TOKEN_BYTES = 32
ACCOUNT_ACTION_REFERENCE_TTL_SECONDS = 900
ACCOUNT_ACTION_REFERENCE_VERSION = "v1"
SESSION_LIFETIME_HOURS = 8
READY_CONNECTION_STATUSES = {"connected"}
RECONNECT_CONNECTION_STATUSES = {"disconnected", "error", "reconnect_required"}

# Email verification before the Instagram-connect step. The token is signed
# with Flask's secret key through itsdangerous (already a Flask dependency,
# so no new package is needed) and expires after 24 hours. Single use is
# enforced server-side: the token carries a random nonce that is stored on
# the user until it is consumed or replaced by a resend.
EMAIL_VERIFICATION_TOKEN_MAX_AGE_SECONDS = 86400
EMAIL_VERIFICATION_SALT = "postenza-email-verification"
EMAIL_VERIFICATION_NONCE_KEY = "email_verification_nonce"
EMAIL_VERIFICATION_RESEND_LIMIT = 3
EMAIL_VERIFICATION_RESEND_WINDOW_SECONDS = 3600

ONBOARDING_USE_CASE_OPTIONS = tuple(zip(USE_CASE_VALUES, (
    "Grow my own Instagram account",
    "Manage a business account",
    "Manage client accounts or agency work",
    "Publish content consistently",
    "Automate comment and DM engagement",
    "Other",
)))
ONBOARDING_MAIN_GOAL_OPTIONS = tuple(zip(MAIN_GOAL_VALUES, (
    "Save time",
    "Generate leads",
    "Increase engagement",
    "Stay consistent with posting",
    "Manage multiple accounts",
    "Other",
)))
ONBOARDING_SOURCE_OPTIONS = tuple(zip(ONBOARDING_SOURCE_VALUES, (
    "Google",
    "Instagram",
    "TikTok",
    "YouTube",
    "Friend or colleague",
    "GitHub",
    "AI tool or ChatGPT",
    "Other",
)))
ONBOARDING_PLAN_OPTIONS = tuple(zip(PLAN_VALUES, (
    ("Trial", "A simple starting preference for exploring Postenza.",
     "$0 — free for 60 days · No card required"),
    ("Starter", "A starter preference for consistent Instagram scheduling.",
     "$9 per month · $7 per month billed annually (−20%)"),
    ("Pro", "A growth-focused preference for established Instagram workflows.",
     "$19 per month · $15 per month billed annually (−20%)"),
    ("Business", "A service-focused preference for teams managing client work.",
     "$39 per month · $31 per month billed annually (−20%)"),
)))

F = TypeVar("F", bound=Callable)


@dataclass
class AuthState:
    session_factory: sessionmaker | None
    legacy_instagram_user_id: str
    configured: bool
    rate_limit_attempts: dict[tuple[str, str], list[float]]
    rate_limit_lock: threading.Lock


class _RegistrationEmailConflict(Exception):
    """Internal signal distinguishing the user-email insert from profile setup."""


def configure_auth(
    app,
    *,
    database_url: str | None,
    secret_key: str | None,
    legacy_instagram_user_id: str,
    production: bool,
) -> None:
    """Configure secure cookies without a production secret-key fallback."""

    configured_secret = str(secret_key or "").strip()
    if configured_secret:
        app.config["SECRET_KEY"] = configured_secret

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(production),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_LIFETIME_HOURS),
    )
    session_factory = create_session_factory(database_url) if database_url else None
    app.extensions["phase3a_auth"] = AuthState(
        session_factory=session_factory,
        legacy_instagram_user_id=str(legacy_instagram_user_id or ""),
        configured=bool(configured_secret and session_factory),
        rate_limit_attempts={},
        rate_limit_lock=threading.Lock(),
    )


def _auth_state() -> AuthState:
    return current_app.extensions["phase3a_auth"]


def authentication_available() -> bool:
    return _auth_state().configured


def csrf_token() -> str:
    """Return a cryptographically random token tied to Flask's signed session."""

    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < CSRF_TOKEN_BYTES:
        token = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_token(provided_token: str | None) -> bool:
    expected_token = session.get(CSRF_SESSION_KEY)
    return bool(
        isinstance(expected_token, str)
        and isinstance(provided_token, str)
        and hmac.compare_digest(expected_token.encode("utf-8"), provided_token.encode("utf-8"))
    )


def request_csrf_token() -> str | None:
    return request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")


def verify_meta_signature() -> bool | None:
    """Verify Meta's POST signature, or return None when not configured yet."""

    app_secret = str(current_app.config.get("META_APP_SECRET") or "")
    if not app_secret:
        return None
    supplied_signature = request.headers.get("X-Hub-Signature-256", "")
    if not supplied_signature.startswith("sha256="):
        return False
    expected_signature = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), request.get_data(cache=True), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature.encode("utf-8"), supplied_signature.encode("utf-8"))


def webhook_signature_verification_active() -> bool:
    return bool(str(current_app.config.get("META_APP_SECRET") or ""))


def get_current_user() -> User | None:
    """Resolve only a validated UUID stored in session; clear invalid state."""

    raw_user_id = session.get(USER_SESSION_KEY)
    try:
        user_id = uuid.UUID(str(raw_user_id))
    except (TypeError, ValueError, AttributeError):
        if raw_user_id is not None:
            session.clear()
        return None

    state = _auth_state()
    if not state.session_factory:
        return None
    try:
        with state.session_factory() as db_session:
            user = db_session.get(User, user_id)
    except SQLAlchemyError:
        return None
    if user is None or user.status != "active":
        session.clear()
        return None
    return user


def current_user_has_legacy_workspace_access() -> bool:
    user = get_current_user()
    state = _auth_state()
    if user is None or not state.session_factory or not state.legacy_instagram_user_id:
        return False
    try:
        with state.session_factory() as db_session:
            account = resolve_owned_instagram_account_by_external_id(
                db_session,
                user_id=user.id,
                instagram_user_id=state.legacy_instagram_user_id,
            )
    except SQLAlchemyError:
        return False
    return account is not None


def current_user_has_legacy_environment_managed_account() -> bool:
    """Return whether the configured legacy identity is still server-managed."""

    user = get_current_user()
    state = _auth_state()
    if user is None or not state.session_factory or not state.legacy_instagram_user_id:
        return False
    try:
        with state.session_factory() as db_session:
            account = db_session.scalar(
                select(InstagramAccount).where(
                    InstagramAccount.user_id == user.id,
                    InstagramAccount.instagram_user_id == state.legacy_instagram_user_id,
                    InstagramAccount.connection_status == "environment_managed",
                )
            )
    except SQLAlchemyError:
        return False
    return account is not None


def action_reference_for_account(
    user_id: object,
    account_id: object,
    purpose: str,
    *,
    secret_key: object,
    now: int | None = None,
) -> str:
    """Create an opaque, purpose-bound, short-lived account action reference."""

    current = int(time.time() if now is None else now)
    expires_at = current + ACCOUNT_ACTION_REFERENCE_TTL_SECONDS
    payload = f"{ACCOUNT_ACTION_REFERENCE_VERSION}|{purpose}|{user_id}|{account_id}|{expires_at}"
    signature = hmac.new(str(secret_key).encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{ACCOUNT_ACTION_REFERENCE_VERSION}.{expires_at}.{encoded}"


def resolve_owned_account_action_reference(
    db_session,
    *,
    user_id: object,
    reference: object,
    purpose: str,
    secret_key: object,
    now: int | None = None,
) -> InstagramAccount | None:
    """Resolve an opaque action only against the current user's owned accounts."""

    if not isinstance(reference, str):
        return None
    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != ACCOUNT_ACTION_REFERENCE_VERSION or not parts[1].isdigit():
        return None
    current = int(time.time() if now is None else now)
    expires_at = int(parts[1])
    if expires_at < current or expires_at > current + ACCOUNT_ACTION_REFERENCE_TTL_SECONDS:
        return None
    for account in list_owned_instagram_accounts(db_session, user_id=user_id):
        expected = action_reference_for_account(
            user_id, account.id, purpose, secret_key=secret_key,
            now=expires_at - ACCOUNT_ACTION_REFERENCE_TTL_SECONDS,
        )
        if hmac.compare_digest(expected, reference):
            return account
    return None


def build_onboarding_account_statuses(
    accounts: Iterable[InstagramAccount],
    *,
    legacy_instagram_user_id: str,
    user_id: object,
    action_secret: object,
) -> list[dict[str, object]]:
    """Create a finite, credential-free status model for owned accounts only."""

    legacy_id = str(legacy_instagram_user_id or "")
    statuses = []
    for account in accounts:
        username = str(account.username or "").strip() or "Instagram account"
        connection_status = str(account.connection_status or "").strip().lower()
        has_connection_error = bool(str(account.last_connection_error_code or "").strip())
        is_legacy_environment_managed = bool(
            legacy_id
            and connection_status == "environment_managed"
            and hmac.compare_digest(str(account.instagram_user_id), legacy_id)
        )
        can_reconnect = False
        can_disconnect = False
        disconnect_action = None
        if is_legacy_environment_managed:
            label = "Workspace managed by server configuration"
            detail = "This is the existing server-managed Instagram workspace."
        elif connection_status == "connected" and has_connection_error:
            label = "Reconnect required"
            detail = "This account needs reconnection before it can be used."
            can_reconnect = True
            # An errored connection can still have a usable credential, so allow
            # the owner to explicitly erase it locally.
            can_disconnect = True
        elif connection_status in RECONNECT_CONNECTION_STATUSES:
            label = "Reconnect required"
            detail = "This account needs reconnection before it can be used."
            can_reconnect = True
            # A disconnected account has already had its credential erased;
            # error/reconnect-required states may still have one to remove.
            can_disconnect = connection_status != "disconnected"
        elif connection_status in READY_CONNECTION_STATUSES:
            label = "Connected and ready"
            detail = "This account is ready for the next connection phase."
            can_disconnect = True
        else:
            label = "Connection setup incomplete"
            detail = "Connection setup will continue in the next connection phase."
        if can_disconnect:
            disconnect_action = action_reference_for_account(
                user_id,
                account.id,
                "disconnect",
                secret_key=action_secret,
            )
        statuses.append(
            {
                "username": username,
                "label": label,
                "detail": detail,
                "can_reconnect": can_reconnect,
                "can_disconnect": can_disconnect,
                "disconnect_action": disconnect_action,
            }
        )
    return statuses


def rotate_authenticated_session(user: User) -> None:
    """Replace session state on login to prevent fixation and rotate CSRF state.

    Only the allowlisted public UI locale survives rotation; every other
    pre-authentication session key, including the previous CSRF token, is
    cleared exactly as before.
    """

    preserved_locale = session.get(LOCALE_SESSION_KEY)
    session.clear()
    if isinstance(preserved_locale, str) and preserved_locale in LOCALE_META:
        session[LOCALE_SESSION_KEY] = preserved_locale
    session[USER_SESSION_KEY] = str(user.id)
    session[CSRF_SESSION_KEY] = secrets.token_urlsafe(CSRF_TOKEN_BYTES)
    session.permanent = True


def clear_authenticated_session() -> None:
    session.clear()


def _rate_limit_allowed(scope: str, limit: int = 5, window_seconds: int = 900) -> bool:
    """Small temporary in-memory rate limiter for unauthenticated auth actions."""

    state = _auth_state()
    client_key = str(request.remote_addr or "unknown")
    now = time.monotonic()
    key = (scope, client_key)
    with state.rate_limit_lock:
        attempts = [
            attempt
            for attempt in state.rate_limit_attempts.get(key, [])
            if now - attempt < window_seconds
        ]
        if len(attempts) >= limit:
            state.rate_limit_attempts[key] = attempts
            return False
        attempts.append(now)
        state.rate_limit_attempts[key] = attempts
    return True


def _email_verification_serializer() -> URLSafeTimedSerializer:
    """Sign verification tokens with Flask's secret key (no new dependency)."""

    return URLSafeTimedSerializer(str(current_app.secret_key), salt=EMAIL_VERIFICATION_SALT)


def _pending_verification_nonce(preferences: object) -> str | None:
    if isinstance(preferences, dict):
        nonce = preferences.get(EMAIL_VERIFICATION_NONCE_KEY)
        if isinstance(nonce, str) and nonce:
            return nonce
    return None


def user_requires_email_verification(user: User | None) -> bool:
    """Return True for new-flow users whose email is still unverified.

    Only users registered through /register (who always get a
    UserOnboardingProfile row) are gated. Accounts created before
    onboarding existed have no profile row and predate email verification,
    so they keep connecting Instagram as before.
    """

    if user is None or user.email_verified_at is not None:
        return False
    state = _auth_state()
    if not state.session_factory:
        return False
    try:
        with state.session_factory() as db_session:
            return onboarding_profile_for_user(db_session, user.id) is not None
    except SQLAlchemyError:
        return False


def issue_email_verification_token(user: User) -> str | None:
    """Create a signed, single-use, expiring token and remember its nonce."""

    state = _auth_state()
    if not state.session_factory:
        return None
    nonce = secrets.token_urlsafe(24)
    token = _email_verification_serializer().dumps(
        {"user_id": str(user.id), "email": user.email, "nonce": nonce}
    )
    try:
        with state.session_factory.begin() as db_session:
            stored = db_session.get(User, user.id)
            if stored is None:
                return None
            preferences = dict(stored.notification_preferences or {})
            preferences[EMAIL_VERIFICATION_NONCE_KEY] = nonce
            stored.notification_preferences = preferences
    except SQLAlchemyError:
        return None
    return token


def _smtp_configuration() -> dict[str, object] | None:
    """Read optional stdlib SMTP settings; None when email is unconfigured."""

    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
    except ValueError:
        port = 587
    return {
        "host": host,
        "port": port,
        "username": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASS", ""),
        "sender": (
            os.environ.get("SMTP_FROM", "").strip()
            or os.environ.get("SMTP_USER", "").strip()
            or "no-reply@postenza.app"
        ),
        "use_tls": os.environ.get("SMTP_USE_TLS", "true").strip().lower()
        not in {"0", "false", "no"},
    }


def _deliver_email(message: EmailMessage) -> bool:
    """Deliver one already-built message with stdlib SMTP; False if unsent.

    Silent on failure (no logging): callers decide what to log. Shares
    the SMTP settings of the verification-mail path below.
    """

    configuration = _smtp_configuration()
    if configuration is None:
        return False
    try:
        if configuration["port"] == 465:
            server = smtplib.SMTP_SSL(
                str(configuration["host"]), int(configuration["port"]), timeout=30
            )
        else:
            server = smtplib.SMTP(
                str(configuration["host"]), int(configuration["port"]), timeout=30
            )
            if configuration["use_tls"]:
                server.starttls()
        with server:
            if configuration["username"] and configuration["password"]:
                server.login(
                    str(configuration["username"]), str(configuration["password"])
                )
            server.send_message(message)
    except (OSError, smtplib.SMTPException):
        return False
    return True


def _send_email_message(address: str, verification_url: str) -> bool:
    """Send one plain-text verification email with stdlib SMTP only."""

    configuration = _smtp_configuration()
    if configuration is None:
        return False
    message = EmailMessage()
    message["From"] = str(configuration["sender"])
    message["To"] = address
    message["Subject"] = "Verify your Postenza email address"
    message.set_content(
        "Welcome to Postenza.\n\n"
        "Verify your email address to connect Instagram:\n"
        f"{verification_url}\n\n"
        "This link expires in 24 hours. If you did not create a Postenza "
        "account, ignore this email.\n"
    )
    try:
        if configuration["port"] == 465:
            server = smtplib.SMTP_SSL(
                str(configuration["host"]), int(configuration["port"]), timeout=30
            )
        else:
            server = smtplib.SMTP(
                str(configuration["host"]), int(configuration["port"]), timeout=30
            )
            if configuration["use_tls"]:
                server.starttls()
        with server:
            if configuration["username"] and configuration["password"]:
                server.login(
                    str(configuration["username"]), str(configuration["password"])
                )
            server.send_message(message)
    except (OSError, smtplib.SMTPException):
        current_app.logger.warning("Email verification message could not be sent.")
        return False
    return True


def send_trial_reminder_email(user: User, days_remaining: int) -> bool:
    """Email a trial-expiry reminder; True only when it was delivered.

    Uses the same stdlib SMTP settings as verification email
    (SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/SMTP_FROM). Returns False
    when SMTP is unconfigured or delivery fails, so the reminder job
    counts it as due-but-unsent and retries on the next run instead of
    marking it sent.
    """

    if not user.email:
        return False
    billing_url = (
        os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        or "https://postenza.app"
    ) + "/app/billing"
    message = EmailMessage()
    configuration = _smtp_configuration()
    message["From"] = (
        str(configuration["sender"]) if configuration else "no-reply@postenza.app"
    )
    message["To"] = str(user.email)
    message["Subject"] = (
        f"Your Postenza free trial ends in {days_remaining} days"
    )
    message.set_content(
        "Hi,\n\n"
        f"Your Postenza free trial ends in {days_remaining} days. "
        "After that, scheduled posts, AI replies and new Instagram "
        "connections stop until you choose a paid plan -- your account "
        "and data stay as they are.\n\n"
        f"Choose a plan here: {billing_url}\n\n"
        "Thanks for trying Postenza.\n"
    )
    delivered = _deliver_email(message)
    if not delivered:
        current_app.logger.warning("Trial reminder email could not be sent.")
    return delivered


def send_email_verification(user: User) -> str:
    """Issue a token and deliver it; 'sent', 'logged', or 'unavailable'.

    When no SMTP server is configured the verification link is written to
    the server log instead, so the flow still works in development. The
    token itself is never rendered on a user-facing page.
    """

    token = issue_email_verification_token(user)
    if token is None:
        return "unavailable"
    verification_url = url_for("verify_email", token=token, _external=True)
    if _send_email_message(str(user.email), verification_url):
        return "sent"
    if _smtp_configuration() is None:
        current_app.logger.warning(
            "SMTP is not configured; email verification link for %s: %s",
            user.email,
            verification_url,
        )
        return "logged"
    return "unavailable"


def smtp_configured() -> bool:
    """Return True when an SMTP host is configured for verification email."""

    return _smtp_configuration() is not None


def _validate_registration(display_name: str, email: str, password: str) -> str | None:
    if not display_name or len(display_name) > 80:
        return "Enter a display name of 80 characters or fewer."
    if not email or len(email) > 254 or "@" not in email or email.startswith("@"):
        return "Enter a valid email address."
    if len(password) < 12:
        return "Use a password with at least 12 characters."
    return None


def _auth_ui_text(message, **values):
    return translate(session_locale(session), message, **values)


SUPPORT_EMAIL = "imran-ichiigo@hotmail.com"


def _safe_locale_return_path() -> str:
    """Return the current page path for locale switches, with a safe fallback."""

    request_path = request.path
    if (
        isinstance(request_path, str)
        and request_path.startswith("/")
        and not request_path.startswith("//")
        and "\\" not in request_path
    ):
        return request_path
    return url_for("login")


def _resolve_locale_return_path(raw_next: object) -> str:
    """Reject arbitrary or external locale return destinations before redirecting."""

    fallback = url_for("login")
    if not isinstance(raw_next, str):
        return fallback
    candidate = raw_next.strip()
    if (
        not candidate
        or not candidate.startswith("/")
        or candidate.startswith("//")
        or candidate.startswith("/\\")
        or "\\" in candidate
    ):
        return fallback
    parts = urlsplit(candidate)
    if parts.netloc or parts.scheme:
        return fallback
    return candidate


def _auth_shell_context() -> dict[str, object]:
    """Shared presentation context for the Postenza auth/onboarding shell."""

    locale = locale_context(session_locale(session))
    home_endpoint = "home" if "home" in current_app.view_functions else None
    return {
        "csrf_token": csrf_token(),
        "locale": locale,
        "locale_options": LOCALE_META,
        "locale_return_path": _safe_locale_return_path(),
        "auth_home_url": url_for(home_endpoint) if home_endpoint else "/",
        "terms_url": url_for("terms") if "terms" in current_app.view_functions else "/terms",
        "privacy_url": url_for("privacy") if "privacy" in current_app.view_functions else "/privacy",
        "support_email": SUPPORT_EMAIL,
        "t": lambda key, **values: translate(locale["code"], key, **values),
    }


def _auth_template(template_name: str, *, status: int = 200, error: str | None = None):
    return render_template(template_name, error=error, **_auth_shell_context()), status


def user_has_connected_instagram_account(user: User | None) -> bool:
    """Return True if the user owns at least one connected and error-free Instagram account."""
    if user is None:
        return False
    state = _auth_state()
    if not state.session_factory:
        return False
    try:
        with state.session_factory() as db_session:
            accounts = list_owned_instagram_accounts(db_session, user_id=user.id)
            for account in accounts:
                status = str(account.connection_status or "").strip().lower()
                has_error = bool(str(account.last_connection_error_code or "").strip())
                if status in READY_CONNECTION_STATUSES and not has_error:
                    return True
    except SQLAlchemyError:
        return False
    return False


def user_has_completed_onboarding(user: User | None) -> bool:
    """Return persisted completion, with missing rows reserved for legacy users."""

    if user is None:
        return False
    state = _auth_state()
    if not state.session_factory:
        return False
    try:
        with state.session_factory() as db_session:
            return user_onboarding_is_complete(db_session, user.id)
    except SQLAlchemyError:
        return False


def post_login_destination(user: User | None) -> str:
    """Require onboarding before applying the existing connection destination."""
    if not user_has_completed_onboarding(user):
        return url_for("onboarding")
    if current_user_has_legacy_workspace_access():
        # The server-managed legacy workspace is now a tenant workspace:
        # send the owner to the new tenant dashboard. The legacy /dashboard
        # route is retired and redirects to /app, so the /dashboard URL is
        # kept only as the fallback destination for apps without the tenant
        # dashboard registered (it still lands on /app via the redirect).
        if "tenant_dashboard.app_overview" in current_app.view_functions:
            return url_for("tenant_dashboard.app_overview")
        return url_for("dashboard")
    if user_has_connected_instagram_account(user):
        if "tenant_dashboard.app_overview" in current_app.view_functions:
            return url_for("tenant_dashboard.app_overview")
        return "/app"
    return url_for("onboarding")


def _verify_email_error(message: str):
    """Render the verification page with an error and a resend action."""

    return (
        render_template(
            "auth_verify_email.html",
            error=message,
            current_user=get_current_user(),
            **_auth_shell_context(),
        ),
        400,
    )


def _verify_email_success_redirect():
    """Send a freshly verified user onward; logged-out users sign in first."""

    user = get_current_user()
    if user is None:
        return redirect(url_for("login"))
    return redirect(post_login_destination(user))


def register_auth_routes(app) -> None:
    """Register only additive Phase 3A browser authentication routes."""

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if not authentication_available():
            return _unavailable_response()
        if request.method == "GET":
            return _auth_template("register.html")
        if not validate_csrf_token(request_csrf_token()):
            return _auth_template("register.html", status=400, error="Invalid form token. Refresh and try again.")
        if not _rate_limit_allowed("register"):
            return _auth_template("register.html", status=429, error="Too many attempts. Try again later.")

        display_name = str(request.form.get("display_name") or "").strip()
        email = str(request.form.get("email") or "").strip().lower()
        password = str(request.form.get("password") or "")
        validation_error = _validate_registration(display_name, email, password)
        if validation_error:
            return _auth_template("register.html", status=400, error=validation_error)

        state = _auth_state()
        try:
            with state.session_factory.begin() as db_session:  # type: ignore[union-attr]
                user = User(
                    email=email,
                    display_name=display_name,
                    password_hash=generate_password_hash(password),
                    # The 60-day free trial starts at registration.
                    trial_started_at=datetime.now(timezone.utc),
                )
                db_session.add(user)
                try:
                    db_session.flush()
                except IntegrityError as error:
                    raise _RegistrationEmailConflict from error
                db_session.add(UserOnboardingProfile(user_id=user.id))
                db_session.flush()
                user_id = user.id
        except _RegistrationEmailConflict:
            return _auth_template("register.html", status=409, error="An account with that email already exists.")
        except SQLAlchemyError:
            return _unavailable_response()

        with state.session_factory() as db_session:  # type: ignore[union-attr]
            user = db_session.get(User, user_id)
            if user is None:
                return _unavailable_response()
            rotate_authenticated_session(user)
            # A new account must verify its email before the Instagram
            # step; delivery is best-effort and never blocks registration.
            send_email_verification(user)
        return redirect(url_for("onboarding"))

    @app.get("/auth/verify-email")
    def verify_email():
        """Consume a signed verification link; login is not required."""

        if not authentication_available():
            return _unavailable_response()
        token = str(request.args.get("token") or "")
        try:
            payload = _email_verification_serializer().loads(
                token, max_age=EMAIL_VERIFICATION_TOKEN_MAX_AGE_SECONDS
            )
        except SignatureExpired:
            return _verify_email_error(
                "This verification link has expired. Request a new one below."
            )
        except BadSignature:
            return _verify_email_error(
                "This verification link is invalid. Request a new one below."
            )
        if not isinstance(payload, dict):
            return _verify_email_error(
                "This verification link is invalid. Request a new one below."
            )
        user_id = payload.get("user_id")
        email = payload.get("email")
        nonce = payload.get("nonce")
        try:
            user_uuid = uuid.UUID(str(user_id))
        except (TypeError, ValueError, AttributeError):
            return _verify_email_error(
                "This verification link is invalid. Request a new one below."
            )
        if not isinstance(email, str) or not isinstance(nonce, str):
            return _verify_email_error(
                "This verification link is invalid. Request a new one below."
            )
        state = _auth_state()
        if state.session_factory is None:
            return _unavailable_response()
        try:
            with state.session_factory.begin() as db_session:
                user = db_session.get(User, user_uuid)
                if user is None or user.email != email or user.status != "active":
                    return _verify_email_error(
                        "This verification link is invalid. Request a new one below."
                    )
                if user.email_verified_at is not None:
                    return _verify_email_success_redirect()
                if _pending_verification_nonce(user.notification_preferences) != nonce:
                    return _verify_email_error(
                        "This verification link has already been used or replaced. "
                        "Request a new one below."
                    )
                user.email_verified_at = datetime.now(timezone.utc)
                preferences = dict(user.notification_preferences or {})
                preferences.pop(EMAIL_VERIFICATION_NONCE_KEY, None)
                user.notification_preferences = preferences
        except SQLAlchemyError:
            return _unavailable_response()
        return _verify_email_success_redirect()

    @app.post("/auth/verify-email/resend")
    @login_required
    def resend_verification_email():
        if not validate_csrf_token(request_csrf_token()):
            return _auth_ui_text("Invalid form token."), 400
        user = get_current_user()
        if user is None:
            return _unauthenticated_response()
        if user.email_verified_at is not None:
            return redirect(url_for("onboarding"), code=303)
        if not _rate_limit_allowed(
            "resend-verification-email",
            limit=EMAIL_VERIFICATION_RESEND_LIMIT,
            window_seconds=EMAIL_VERIFICATION_RESEND_WINDOW_SECONDS,
        ):
            return _auth_ui_text("Too many verification emails requested. Try again later."), 429
        send_email_verification(user)
        return redirect(url_for("onboarding"), code=303)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not authentication_available():
            return _unavailable_response()
        user = get_current_user()
        if request.method == "GET" and user is not None:
            return redirect(post_login_destination(user))
        if request.method == "GET":
            return _auth_template("login.html")
        if not validate_csrf_token(request_csrf_token()):
            return _auth_template("login.html", status=400, error="Invalid form token. Refresh and try again.")
        if not _rate_limit_allowed("login"):
            return _auth_template("login.html", status=429, error="Too many attempts. Try again later.")

        email = str(request.form.get("email") or "").strip().lower()
        password = str(request.form.get("password") or "")
        try:
            with _auth_state().session_factory() as db_session:  # type: ignore[union-attr]
                user = db_session.scalar(select(User).where(User.email == email))
        except SQLAlchemyError:
            return _unavailable_response()
        if (
            user is None
            or user.status != "active"
            or not user.password_hash
            or not check_password_hash(user.password_hash, password)
        ):
            return _auth_template("login.html", status=401, error="Invalid email or password.")
        rotate_authenticated_session(user)
        return redirect(post_login_destination(user))

    @app.post("/auth/locale")
    def auth_locale():
        """Store an allowlisted public UI locale and return to the current page."""

        if not validate_csrf_token(request_csrf_token()):
            return _auth_template("login.html", status=400, error="Invalid form token. Refresh and try again.")
        session[LOCALE_SESSION_KEY] = normalize_locale(request.form.get("locale"))
        return redirect(_resolve_locale_return_path(request.form.get("next")), code=303)

    @app.post("/logout")
    @login_required
    def logout():
        if not validate_csrf_token(request_csrf_token()):
            return _auth_ui_text("Invalid form token."), 400
        clear_authenticated_session()
        return redirect(url_for("login"))

    @app.post("/connect/instagram")
    @login_required
    def start_instagram_connection():
        if not validate_csrf_token(request_csrf_token()):
            return _auth_ui_text("Invalid form token."), 400
        user = get_current_user()
        if user is None:
            return _unauthenticated_response()
        if not user_has_completed_onboarding(user):
            return redirect(url_for("onboarding"))
        if user_requires_email_verification(user):
            return _auth_ui_text(
                "Verify your email before connecting Instagram. Open the "
                "verification link we sent, or request a new one from onboarding."
            ), 403
        if current_user_has_legacy_environment_managed_account():
            return _auth_ui_text("Instagram connection is not available for this account."), 403
        try:
            redirect_uri = resolve_instagram_oauth_redirect_uri(
                request.host,
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
            )
            configuration = oauth_configuration_from_environment(
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
                redirect_uri=redirect_uri,
            )
        except OAuthConfigurationError:
            return _auth_ui_text("Instagram connection configuration is unavailable."), 503
        state = issue_instagram_oauth_state(user.id)
        return redirect(configuration.authorization_url(state))

    @app.get("/connect/instagram/callback")
    @login_required
    def complete_instagram_connection():
        user = get_current_user()
        if user is None:
            return _unauthenticated_response()
        try:
            consume_instagram_oauth_state(request.args.get("state"), user.id)
        except OAuthStateError as error:
            current_app.logger.warning(error.diagnostic_stage)
            return _auth_ui_text("Instagram authorization could not be completed."), 400

        if any(request.args.get(name) for name in ("error", "error_reason", "error_description")):
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_PROVIDER_DENIED)
            return _auth_ui_text("Instagram authorization could not be completed."), 400
        authorization_code = request.args.get("code")
        if not isinstance(authorization_code, str) or not authorization_code:
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_AUTHORIZATION_CODE_MISSING)
            return _auth_ui_text("Instagram authorization could not be completed."), 400
        try:
            redirect_uri = resolve_instagram_oauth_redirect_uri(
                request.host,
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
            )
            configuration = oauth_configuration_from_environment(
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
                redirect_uri=redirect_uri,
            )
            token_result = complete_instagram_token_exchange(authorization_code, configuration)
            discovery = discover_instagram_account(token_result)
            keyring = credential_keyring_from_environment()
            state = _auth_state()
            if state.session_factory is None:
                return _unavailable_response()
            account = complete_discovered_instagram_connection(
                state.session_factory,
                user_id=user.id,
                legacy_instagram_user_id=state.legacy_instagram_user_id,
                discovery=discovery,
                token_result=token_result,
                keyring=keyring,
            )
        except (OAuthConfigurationError, CredentialConfigurationError):
            return _auth_ui_text("Instagram connection configuration is unavailable."), 503
        except OAuthExchangeError as error:
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_TOKEN_EXCHANGE_FAILED)
            if error.diagnostic_stage == OAUTH_CALLBACK_STAGE_PERMISSIONS_INVALID:
                current_app.logger.warning(OAUTH_CALLBACK_STAGE_PERMISSIONS_FAILED)
            if error.diagnostic_stage == OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED:
                diagnostics = error.safe_diagnostics
                current_app.logger.warning(
                    f"{error.diagnostic_stage} "
                    f"provider_http_status={diagnostics.get('provider_http_status')} "
                    f"provider_error_classification={diagnostics.get('provider_error_classification')} "
                    f"provider_error_type={diagnostics.get('provider_error_type')} "
                    f"provider_error_code={diagnostics.get('provider_error_code')} "
                    f"provider_error_subcode={diagnostics.get('provider_error_subcode')}"
                )
            else:
                current_app.logger.warning(error.diagnostic_stage)
            return _auth_ui_text("Instagram connection could not be completed."), 502
        except OAuthDiscoveryError as error:
            current_app.logger.warning(error.diagnostic_stage)
            return _auth_ui_text("Instagram connection could not be completed."), 502
        except InstagramConnectionError:
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_CREDENTIAL_PERSISTENCE_FAILED)
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED)
            return _auth_ui_text("Instagram connection could not be completed."), 502
        except SQLAlchemyError:
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_FINAL_TRANSACTION_FAILED)
            return _auth_ui_text("Instagram connection could not be completed."), 502
        if not user_has_completed_onboarding(user):
            target = url_for("onboarding")
        else:
            account_ref = action_reference_for_account(
                user.id,
                account.id,
                "tenant-dashboard-select",
                secret_key=current_app.secret_key,
            )
            if "tenant_dashboard.app_overview" in current_app.view_functions:
                target = url_for("tenant_dashboard.app_overview", account=account_ref)
            else:
                target = f"/app?account={account_ref}"
        if redirect_uri == f"{PUBLIC_BASE_URL}/connect/instagram/callback":
            target = f"{PUBLIC_BASE_URL}{target}"
        return redirect(target)

    @app.post("/connect/instagram/disconnect")
    @login_required
    def disconnect_instagram_connection():
        """Erase one owned OAuth credential through an opaque, CSRF-bound action."""

        if not validate_csrf_token(request_csrf_token()):
            return _auth_ui_text("Invalid form token."), 400
        user = get_current_user()
        state = _auth_state()
        if user is None:
            return _unauthenticated_response()
        if state.session_factory is None:
            return _unavailable_response()
        try:
            with state.session_factory() as db_session:
                account = resolve_owned_account_action_reference(
                    db_session,
                    user_id=user.id,
                    reference=request.form.get("account_action"),
                    purpose="disconnect",
                    secret_key=current_app.secret_key,
                )
            if account is None:
                return _auth_ui_text("Instagram connection could not be completed."), 400
            disconnect_owned_instagram_connection(
                state.session_factory,
                user_id=user.id,
                account_id=account.id,
                legacy_instagram_user_id=state.legacy_instagram_user_id,
            )
        except SQLAlchemyError:
            return _unavailable_response()
        except InstagramConnectionError:
            return _auth_ui_text("Instagram connection could not be completed."), 400
        return redirect(url_for("onboarding"))

    @app.route("/onboarding", methods=["GET", "POST"])
    @login_required
    def onboarding():
        user = get_current_user()
        state = _auth_state()
        if user is None or state.session_factory is None:
            return _unavailable_response()
        submitted: dict[str, str] = {}
        error: str | None = None
        status = 200
        if request.method == "POST":
            submitted = {
                key: str(request.form.get(key) or "").strip()[:128]
                for key in (
                    "use_case",
                    "main_goal",
                    "onboarding_source",
                    "selected_plan",
                )
            }
            if not validate_csrf_token(request_csrf_token()):
                error = "Invalid form token. Refresh and try again."
                status = 400
            else:
                try:
                    with state.session_factory.begin() as db_session:
                        profile = onboarding_profile_for_user(db_session, user.id)
                        if profile is None:
                            raise OnboardingValidationError(
                                "Legacy users have no questionnaire step to update."
                            )
                        apply_onboarding_step(
                            profile,
                            request.form.get("step"),
                            submitted,
                        )
                        completed = profile.completed_at is not None
                except OnboardingValidationError:
                    error = "Choose one option for each question."
                    status = 400
                except SQLAlchemyError:
                    return _unavailable_response()
                else:
                    destination = (
                        post_login_destination(user)
                        if completed
                        else url_for("onboarding")
                    )
                    return redirect(destination, code=303)

        try:
            with state.session_factory() as db_session:
                profile = onboarding_profile_for_user(db_session, user.id)
                step = "instagram" if profile is None else expected_onboarding_step(profile)
                accounts = (
                    list_owned_instagram_accounts(db_session, user_id=user.id)
                    if step == "instagram"
                    else []
                )
        except SQLAlchemyError:
            return _unavailable_response()
        email_verification_required = (
            profile is not None and user.email_verified_at is None
        )
        return render_template(
            "onboarding.html",
            current_user=user,
            onboarding_step=step,
            email_verification_required=email_verification_required,
            email_verification_smtp_configured=smtp_configured(),
            onboarding_step_number={
                "profile": 1,
                "source": 2,
                "plan": 3,
                "instagram": 4,
            }[step],
            use_case_options=ONBOARDING_USE_CASE_OPTIONS,
            main_goal_options=ONBOARDING_MAIN_GOAL_OPTIONS,
            source_options=ONBOARDING_SOURCE_OPTIONS,
            plan_options=ONBOARDING_PLAN_OPTIONS,
            submitted=submitted,
            error=error,
            account_statuses=build_onboarding_account_statuses(
                accounts,
                legacy_instagram_user_id=state.legacy_instagram_user_id,
                user_id=user.id,
                action_secret=current_app.secret_key,
            ),
            **_auth_shell_context(),
        ), status


def install_auth_route_guard(app) -> None:
    """Protect the existing legacy workspace without changing individual routes."""

    public_endpoints = {
        "home", "privacy", "terms", "deletion", "verify_webhook", "receive_webhook",
        "login", "register", "logout", "onboarding", "auth_locale", "static",
    }
    disabled_post_endpoints = {
        "approve_dashboard_draft", "publish_dashboard_draft", "schedule_dashboard_draft",
    }

    @app.before_request
    def require_legacy_workspace_access():
        endpoint = request.endpoint
        if endpoint is None or endpoint in public_endpoints or endpoint in disabled_post_endpoints:
            return None
        # /dashboard stays guarded even though it now redirects to /app:
        # unauthenticated visitors go to login and non-legacy users go to
        # onboarding instead of reaching the app overview.
        protected_path = (
            request.path == "/dashboard"
            or request.path.startswith("/api/")
            or request.path.startswith("/draft-image/")
        )
        if not protected_path:
            return None
        if not authentication_available():
            return _unavailable_response()
        if get_current_user() is None:
            return _unauthenticated_response()
        if not current_user_has_legacy_workspace_access():
            if request.path.startswith("/api/") or request.path.startswith("/draft-image/"):
                return jsonify(error="Legacy workspace access is not available for this account."), 403
            return redirect(url_for("onboarding"))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not validate_csrf_token(request_csrf_token()):
            return jsonify(error="Invalid CSRF token."), 400
        return None


def _unauthenticated_response():
    if request.path.startswith("/api/") or request.path.startswith("/draft-image/"):
        return jsonify(error="Authentication is required."), 401
    return redirect(url_for("login"))


def _unavailable_response():
    if request.path.startswith("/api/") or request.path.startswith("/draft-image/"):
        return jsonify(error="Authentication service is unavailable."), 503
    return _auth_ui_text("Authentication service is unavailable."), 503


def login_required(function: F) -> F:
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not authentication_available():
            return _unavailable_response()
        if get_current_user() is None:
            return _unauthenticated_response()
        return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def legacy_workspace_required(function: F) -> F:
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not authentication_available():
            return _unavailable_response()
        if get_current_user() is None:
            return _unauthenticated_response()
        if not current_user_has_legacy_workspace_access():
            if request.path.startswith("/api/") or request.path.startswith("/draft-image/"):
                return jsonify(error="Legacy workspace access is not available for this account."), 403
            return redirect(url_for("onboarding"))
        return function(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
