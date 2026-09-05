"""Authentication primitives for the additive Phase 3A Flask flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from functools import wraps
from typing import Callable, TypeVar

from flask import current_app, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash
from dashboard.i18n import locale_context, session_locale, translate

from database.credentials import CredentialConfigurationError, credential_keyring_from_environment
from database.instagram_connection import (
    InstagramConnectionError,
    complete_discovered_instagram_connection,
    disconnect_owned_instagram_connection,
)
from database.models import InstagramAccount, User
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
    OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED,
    OAUTH_CALLBACK_STAGE_LONG_EXCHANGE_FAILED,
    complete_instagram_token_exchange,
    consume_instagram_oauth_state,
    discover_instagram_account,
    issue_instagram_oauth_state,
    oauth_configuration_from_environment,
)


CSRF_SESSION_KEY = "_csrf_token"
USER_SESSION_KEY = "user_id"
CSRF_TOKEN_BYTES = 32
ACCOUNT_ACTION_REFERENCE_TTL_SECONDS = 900
ACCOUNT_ACTION_REFERENCE_VERSION = "v1"
SESSION_LIFETIME_HOURS = 8
READY_CONNECTION_STATUSES = {"connected"}
RECONNECT_CONNECTION_STATUSES = {"disconnected", "error", "reconnect_required"}

F = TypeVar("F", bound=Callable)


@dataclass
class AuthState:
    session_factory: sessionmaker | None
    legacy_instagram_user_id: str
    configured: bool
    rate_limit_attempts: dict[tuple[str, str], list[float]]
    rate_limit_lock: threading.Lock


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
        and hmac.compare_digest(expected_token, provided_token)
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
    return hmac.compare_digest(expected_signature, supplied_signature)


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
    if user is None:
        session.clear()
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
            label = "Legacy workspace managed by server configuration"
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
    """Replace session state on login to prevent fixation and rotate CSRF state."""

    session.clear()
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


def _auth_template(template_name: str, *, status: int = 200, error: str | None = None):
    return render_template(template_name, csrf_token=csrf_token(), error=error), status


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


def post_login_destination(user: User | None) -> str:
    """Direct connected users to /app and disconnected users to onboarding."""
    if user_has_connected_instagram_account(user):
        if "tenant_dashboard.app_overview" in current_app.view_functions:
            return url_for("tenant_dashboard.app_overview")
        return "/app"
    return url_for("onboarding")


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
                )
                db_session.add(user)
                db_session.flush()
                user_id = user.id
        except IntegrityError:
            return _auth_template("register.html", status=409, error="An account with that email already exists.")
        except SQLAlchemyError:
            return _unavailable_response()

        with state.session_factory() as db_session:  # type: ignore[union-attr]
            user = db_session.get(User, user_id)
            if user is None:
                return _unavailable_response()
            rotate_authenticated_session(user)
        return redirect(url_for("onboarding"))

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
        if user is None or not check_password_hash(user.password_hash, password):
            return _auth_template("login.html", status=401, error="Invalid email or password.")
        rotate_authenticated_session(user)
        return redirect(post_login_destination(user))

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
        if current_user_has_legacy_workspace_access():
            return _auth_ui_text("Instagram connection is not available for this account."), 403
        try:
            configuration = oauth_configuration_from_environment(
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
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
        except OAuthStateError:
            return _auth_ui_text("Instagram authorization could not be completed."), 400

        if any(request.args.get(name) for name in ("error", "error_reason", "error_description")):
            return _auth_ui_text("Instagram authorization could not be completed."), 400
        authorization_code = request.args.get("code")
        if not isinstance(authorization_code, str) or not authorization_code:
            return _auth_ui_text("Instagram authorization could not be completed."), 400
        try:
            configuration = oauth_configuration_from_environment(
                production=bool(current_app.config.get("SESSION_COOKIE_SECURE")),
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
            current_app.logger.warning(OAUTH_CALLBACK_STAGE_CONNECTION_PRECOMMIT_FAILED)
            return _auth_ui_text("Instagram connection could not be completed."), 502
        account_ref = action_reference_for_account(
            user.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=current_app.secret_key,
        )
        if "tenant_dashboard.app_overview" in current_app.view_functions:
            return redirect(url_for("tenant_dashboard.app_overview", account=account_ref))
        return redirect(f"/app?account={account_ref}")

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

    @app.get("/onboarding")
    @login_required
    def onboarding():
        user = get_current_user()
        state = _auth_state()
        if user is None or state.session_factory is None:
            return _unavailable_response()
        try:
            with state.session_factory() as db_session:
                accounts = list_owned_instagram_accounts(db_session, user_id=user.id)
        except SQLAlchemyError:
            return _unavailable_response()
        return render_template(
            "onboarding.html",
            locale=locale_context(session_locale(session)),
            t=_auth_ui_text,
            current_user=user,
            csrf_token=csrf_token(),
            account_statuses=build_onboarding_account_statuses(
                accounts,
                legacy_instagram_user_id=state.legacy_instagram_user_id,
                user_id=user.id,
                action_secret=current_app.secret_key,
            ),
        )


def install_auth_route_guard(app) -> None:
    """Protect the existing legacy workspace without changing individual routes."""

    public_endpoints = {
        "home", "privacy", "terms", "deletion", "verify_webhook", "receive_webhook",
        "login", "register", "logout", "onboarding", "static",
    }
    disabled_post_endpoints = {
        "approve_dashboard_draft", "publish_dashboard_draft", "schedule_dashboard_draft",
    }

    @app.before_request
    def require_legacy_workspace_access():
        endpoint = request.endpoint
        if endpoint is None or endpoint in public_endpoints or endpoint in disabled_post_endpoints:
            return None
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
