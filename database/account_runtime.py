"""Trusted server-only runtime credential resolution for Instagram accounts."""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .credentials import (
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialKeyring,
    CredentialNotFoundError,
    credential_keyring_from_environment,
    decrypt_access_token,
)
from .models import InstagramAccount, InstagramCredential


INSTAGRAM_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")
OAUTH_CONNECTION_STATUS = "connected"
USABLE_CREDENTIAL_STATUS = "valid"
LEGACY_CONNECTION_STATUS = "environment_managed"
OAUTH_GRAPH_PROFILE_URL = "https://graph.instagram.com/v25.0/me"
OAUTH_GRAPH_PROFILE_TIMEOUT_SECONDS = 15
WEBHOOK_IDENTITY_CLASSIFICATION_CONNECTED_OAUTH = "connected_oauth"
WEBHOOK_IDENTITY_CLASSIFICATION_OTHER_KNOWN = "other_known"
WEBHOOK_IDENTITY_CLASSIFICATION_UNKNOWN = "unknown"
WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE = "indeterminate"


class RuntimeCredentialUnavailableError(ValueError):
    """A generic, credential-safe runtime resolution failure."""


class RuntimeAccountRouteUnavailableError(ValueError):
    """A generic, token-free webhook account routing failure."""


@dataclass(frozen=True)
class InstagramAccountRuntime:
    """Minimal trusted-only context for one immediate Graph API operation."""

    instagram_account_id: uuid.UUID
    instagram_user_id: str
    connection_mode: str
    _access_token: str = field(repr=False, compare=False)

    @property
    def access_token(self) -> str:
        """Return plaintext only to the immediate trusted backend caller."""

        return self._access_token


@dataclass(frozen=True)
class InstagramAccountRoute:
    """Token-free immutable routing metadata for a verified webhook entry."""

    instagram_account_id: uuid.UUID
    connection_mode: str


def _unavailable() -> RuntimeCredentialUnavailableError:
    return RuntimeCredentialUnavailableError("Instagram runtime credential is unavailable.")


def _route_unavailable() -> RuntimeAccountRouteUnavailableError:
    return RuntimeAccountRouteUnavailableError("Instagram account route is unavailable.")


def _normalized_external_id(value: object) -> str | None:
    external_id = value if isinstance(value, str) else ""
    external_id = external_id.strip()
    return external_id if INSTAGRAM_EXTERNAL_ID_PATTERN.fullmatch(external_id) else None


def _is_expired(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc) <= now


def _usable_oauth_credential(
    account: InstagramAccount,
    credential: InstagramCredential | None,
    *,
    now: datetime,
) -> bool:
    return bool(
        account.provider == "instagram"
        and
        account.connection_status == OAUTH_CONNECTION_STATUS
        and not str(account.last_connection_error_code or "").strip()
        and not _is_expired(account.token_expires_at, now)
        and credential is not None
        and credential.token_status == USABLE_CREDENTIAL_STATUS
        and credential.revoked_at is None
        and not str(credential.last_error_code or "").strip()
        and isinstance(credential.token_ciphertext, str)
        and bool(credential.token_ciphertext)
        and isinstance(credential.encryption_key_id, str)
        and bool(credential.encryption_key_id)
        and not _is_expired(credential.expires_at, now)
    )


def _usable_oauth_account(account: InstagramAccount, *, now: datetime) -> bool:
    """Return lifecycle-only eligibility without touching credential storage."""

    return bool(
        account.provider == "instagram"
        and account.connection_status == OAUTH_CONNECTION_STATUS
        and not str(account.last_connection_error_code or "").strip()
        and not _is_expired(account.token_expires_at, now)
    )


def instagram_account_route_identity_match_state(
    session: Session,
    *,
    instagram_user_id: object,
) -> str:
    """Return only a finite token-free identity match diagnostic."""

    external_id = _normalized_external_id(instagram_user_id)
    if external_id is None:
        return "indeterminate"
    try:
        account_id = session.scalar(
            select(InstagramAccount.id).where(
                InstagramAccount.instagram_user_id == external_id
            )
        )
        return "True" if account_id is not None else "False"
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        return "indeterminate"


def webhook_identity_matches_single_connected_oauth_graph_profile(
    session: Session,
    *,
    instagram_user_id: object,
    keyring: CredentialKeyring | None = None,
) -> str:
    """Compare a failed webhook identity to one connected OAuth profile only.

    This is diagnostic-only: it never mutates state, emits logs, or returns an
    identifier.  Any unavailable or ambiguous dependency is indeterminate.
    """

    webhook_external_id = _normalized_external_id(instagram_user_id)
    if webhook_external_id is None:
        return "indeterminate"
    try:
        accounts = list(
            session.scalars(
                select(InstagramAccount).where(
                    InstagramAccount.provider == "instagram",
                    InstagramAccount.connection_status == OAUTH_CONNECTION_STATUS,
                )
            )
        )
        if len(accounts) != 1:
            return "indeterminate"
        account = accounts[0]
        runtime = resolve_oauth_instagram_account_runtime(
            session,
            instagram_account_id=account.id,
            instagram_user_id=account.instagram_user_id,
            keyring=keyring,
        )
        response = requests.get(
            OAUTH_GRAPH_PROFILE_URL,
            headers={"Authorization": f"Bearer {runtime.access_token}"},
            params={"fields": "id"},
            timeout=OAUTH_GRAPH_PROFILE_TIMEOUT_SECONDS,
        )
        if not bool(getattr(response, "ok", False)):
            return "indeterminate"
        payload = response.json()
        profile_external_id = _normalized_external_id(
            payload.get("id") if isinstance(payload, dict) else None
        )
        if profile_external_id is None:
            return "indeterminate"
        return "True" if hmac.compare_digest(webhook_external_id, profile_external_id) else "False"
    except Exception:
        return "indeterminate"


def classify_failed_webhook_identity(
    session: Session,
    *,
    instagram_user_id: object,
    keyring: CredentialKeyring | None = None,
) -> str:
    """Return a finite, in-memory classification for a failed webhook route.

    This diagnostic never writes state or emits logs.  It intentionally uses
    no mutable username-to-label mapping: a verified Graph profile match is
    ``connected_oauth`` and any other persisted identity is ``other_known``.
    """

    webhook_external_id = _normalized_external_id(instagram_user_id)
    if webhook_external_id is None:
        return WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE
    try:
        accounts = list(session.scalars(select(InstagramAccount).where(
            InstagramAccount.provider == "instagram"
        )))
        persisted_matches = [
            account for account in accounts
            if hmac.compare_digest(str(account.instagram_user_id), webhook_external_id)
        ]
        if len(persisted_matches) > 1:
            return WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE

        for account in accounts:
            if not _usable_oauth_account(account, now=datetime.now(timezone.utc)):
                continue
            runtime = resolve_oauth_instagram_account_runtime(
                session,
                instagram_account_id=account.id,
                instagram_user_id=account.instagram_user_id,
                keyring=keyring,
            )
            response = requests.get(
                OAUTH_GRAPH_PROFILE_URL,
                headers={"Authorization": f"Bearer {runtime.access_token}"},
                params={"fields": "id"},
                timeout=OAUTH_GRAPH_PROFILE_TIMEOUT_SECONDS,
            )
            if not bool(getattr(response, "ok", False)):
                return WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE
            payload = response.json()
            profile_external_id = _normalized_external_id(
                payload.get("id") if isinstance(payload, dict) else None
            )
            if profile_external_id is None:
                return WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE
            if hmac.compare_digest(webhook_external_id, profile_external_id):
                return WEBHOOK_IDENTITY_CLASSIFICATION_CONNECTED_OAUTH

        if persisted_matches:
            return WEBHOOK_IDENTITY_CLASSIFICATION_OTHER_KNOWN
        return WEBHOOK_IDENTITY_CLASSIFICATION_UNKNOWN
    except Exception:
        return WEBHOOK_IDENTITY_CLASSIFICATION_INDETERMINATE


def resolve_instagram_account_route(
    session: Session,
    *,
    instagram_user_id: object,
    legacy_instagram_user_id: object,
    now: datetime | None = None,
) -> InstagramAccountRoute:
    """Classify one verified webhook account without loading any credential.

    This is deliberately narrower than ``resolve_instagram_account_runtime``:
    it performs no credential query, decryption, keyring lookup, or token
    handling.  OAuth webhook entries are only classified in Phase 4B; their
    Graph operations remain disabled.
    """

    external_id = _normalized_external_id(instagram_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)
    if external_id is None:
        raise _route_unavailable()

    try:
        account = session.scalar(
            select(InstagramAccount).where(InstagramAccount.instagram_user_id == external_id)
        )
        if account is None:
            raise _route_unavailable()

        legacy_id = _normalized_external_id(legacy_instagram_user_id)
        is_exact_legacy_account = bool(
            legacy_id is not None
            and hmac.compare_digest(external_id, legacy_id)
            and hmac.compare_digest(str(account.instagram_user_id), legacy_id)
        )
        if is_exact_legacy_account:
            if account.connection_status != LEGACY_CONNECTION_STATUS:
                raise _route_unavailable()
            return InstagramAccountRoute(
                instagram_account_id=account.id,
                connection_mode="legacy_environment",
            )

        if not _usable_oauth_account(account, now=current_time):
            raise _route_unavailable()
        return InstagramAccountRoute(
            instagram_account_id=account.id,
            connection_mode="oauth",
        )
    except RuntimeAccountRouteUnavailableError:
        raise
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        raise _route_unavailable() from None


def resolve_instagram_account_runtime(
    session: Session,
    *,
    instagram_user_id: object,
    legacy_instagram_user_id: object,
    legacy_access_token: object,
    keyring: CredentialKeyring | None = None,
    now: datetime | None = None,
) -> InstagramAccountRuntime:
    """Resolve one trusted external ID to exactly its usable runtime token.

    This boundary is intentionally read-only and has no Flask, JSON, webhook,
    scheduler, or process-global token dependencies.  Callers must pass an
    external ID obtained from a trusted server-side source such as a verified
    Meta webhook envelope.
    """

    external_id = _normalized_external_id(instagram_user_id)
    legacy_id = _normalized_external_id(legacy_instagram_user_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    else:
        current_time = current_time.astimezone(timezone.utc)
    if external_id is None or legacy_id is None:
        raise _unavailable()

    try:
        account = session.scalar(
            select(InstagramAccount).where(InstagramAccount.instagram_user_id == external_id)
        )
        if account is None:
            raise _unavailable()

        is_exact_legacy_account = bool(
            hmac.compare_digest(external_id, legacy_id)
            and hmac.compare_digest(str(account.instagram_user_id), legacy_id)
        )
        if is_exact_legacy_account:
            token = legacy_access_token if isinstance(legacy_access_token, str) else ""
            if account.connection_status != LEGACY_CONNECTION_STATUS or not token:
                raise _unavailable()
            return InstagramAccountRuntime(
                instagram_account_id=account.id,
                instagram_user_id=external_id,
                connection_mode="legacy_environment",
                _access_token=token,
            )

        credential = session.scalar(
            select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == account.id
            )
        )
        if not _usable_oauth_credential(account, credential, now=current_time):
            raise _unavailable()
        active_keyring = keyring if keyring is not None else credential_keyring_from_environment()
        token = decrypt_access_token(
            credential.token_ciphertext,
            credential.encryption_key_id,
            active_keyring,
        )
        if not token:
            raise _unavailable()
        return InstagramAccountRuntime(
            instagram_account_id=account.id,
            instagram_user_id=external_id,
            connection_mode="oauth",
            _access_token=token,
        )
    except RuntimeCredentialUnavailableError:
        raise
    except (
        CredentialConfigurationError,
        CredentialDecryptionError,
        CredentialNotFoundError,
        SQLAlchemyError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        raise _unavailable() from None


def resolve_oauth_instagram_account_runtime(
    session: Session,
    *,
    instagram_account_id: object,
    instagram_user_id: object,
    keyring: CredentialKeyring | None = None,
    now: datetime | None = None,
) -> InstagramAccountRuntime:
    """Resolve only a connected OAuth account; legacy credentials are impossible."""

    external_id = _normalized_external_id(instagram_user_id)
    try:
        account_id = uuid.UUID(str(instagram_account_id))
    except (TypeError, ValueError, AttributeError):
        raise _unavailable() from None
    current_time = now or datetime.now(timezone.utc)
    if external_id is None:
        raise _unavailable()
    try:
        account = session.scalar(select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.instagram_user_id == external_id,
        ))
        credential = session.scalar(select(InstagramCredential).where(
            InstagramCredential.instagram_account_id == account_id
        )) if account is not None else None
        if account is None or not _usable_oauth_credential(account, credential, now=current_time):
            raise _unavailable()
        active_keyring = keyring if keyring is not None else credential_keyring_from_environment()
        token = decrypt_access_token(credential.token_ciphertext, credential.encryption_key_id, active_keyring)
        if not token:
            raise _unavailable()
        return InstagramAccountRuntime(account.id, external_id, "oauth", token)
    except RuntimeCredentialUnavailableError:
        raise
    except (CredentialConfigurationError, CredentialDecryptionError, CredentialNotFoundError, SQLAlchemyError, AttributeError, TypeError, ValueError):
        raise _unavailable() from None
