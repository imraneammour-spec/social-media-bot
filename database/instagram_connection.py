"""Atomic, server-only persistence for a discovered Instagram connection."""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from instagram_oauth import InstagramAccountDiscovery, InstagramOAuthTokenResult, normalized_expires_in

from .credentials import (
    CredentialConfigurationError,
    CredentialEncryptionError,
    CredentialPersistenceError,
    CredentialKeyring,
    store_encrypted_credential,
)
from .models import InstagramAccount, InstagramCredential
from .ownership import resolve_owned_instagram_account


INSTAGRAM_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")


class InstagramConnectionError(ValueError):
    """Raised without account, token, provider, or database details."""


def _connection_error() -> InstagramConnectionError:
    return InstagramConnectionError("Instagram connection could not be completed.")


def _normalized_user_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _connection_error() from None


def _validated_discovery(discovery: object) -> tuple[str, str | None]:
    instagram_user_id = getattr(discovery, "instagram_user_id", None)
    if not isinstance(instagram_user_id, str) or not INSTAGRAM_EXTERNAL_ID_PATTERN.fullmatch(
        instagram_user_id
    ):
        raise _connection_error()

    username = getattr(discovery, "username", None)
    if isinstance(username, str):
        username = username.strip() or None
    else:
        username = None
    if username is not None and len(username) > 255:
        username = None
    return instagram_user_id, username


def _validated_token_result(token_result: object) -> tuple[str, int | None, list[str] | None]:
    token = getattr(token_result, "long_lived_access_token", None)
    if not isinstance(token, str) or not token:
        raise _connection_error()

    expires_in = normalized_expires_in(getattr(token_result, "expires_in", None))
    scopes = getattr(token_result, "scopes", None)
    if (
        not isinstance(scopes, tuple)
        or not scopes
        or len(scopes) > 64
        or not all(
            isinstance(scope, str)
            and 0 < len(scope) <= 128
            and all(character.islower() or character.isdigit() or character == "_" for character in scope)
            for scope in scopes
        )
    ):
        scopes = None
    else:
        scopes = list(scopes)
    return token, expires_in, scopes


def _utc_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if not isinstance(current, datetime):
        raise _connection_error()
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _expiry_at(now: datetime, expires_in: int | None) -> datetime | None:
    if expires_in is None:
        return None
    try:
        return now + timedelta(seconds=expires_in)
    except (OverflowError, TypeError, ValueError):
        return None


def persist_discovered_instagram_connection(
    session_factory: sessionmaker,
    *,
    user_id: object,
    legacy_instagram_user_id: object,
    discovery: InstagramAccountDiscovery,
    token_result: InstagramOAuthTokenResult,
    keyring: CredentialKeyring,
    now: datetime | None = None,
) -> InstagramAccount:
    """Create or reconnect one owned account and credential in one transaction.

    Every mutation, including replacement of an existing encrypted credential,
    is part of the same transaction. Any exception therefore leaves the prior
    account/credential relationship and ciphertext untouched.
    """

    normalized_user_id = _normalized_user_id(user_id)
    instagram_user_id, username = _validated_discovery(discovery)
    token, expires_in, scopes = _validated_token_result(token_result)
    legacy_id = str(legacy_instagram_user_id or "").strip()
    if legacy_id and hmac.compare_digest(instagram_user_id, legacy_id):
        raise _connection_error()

    current_time = _utc_now(now)
    expiry = _expiry_at(current_time, expires_in)
    try:
        with session_factory.begin() as db_session:
            account = db_session.scalar(
                select(InstagramAccount).where(
                    InstagramAccount.instagram_user_id == instagram_user_id
                )
            )
            if account is not None and account.user_id != normalized_user_id:
                raise _connection_error()
            if account is None:
                account = InstagramAccount(
                    user_id=normalized_user_id,
                    instagram_user_id=instagram_user_id,
                )
                db_session.add(account)

            if username is not None:
                account.username = username
            account.provider = "instagram"
            account.connection_status = "connected"
            account.connected_at = current_time
            account.disconnected_at = None
            account.last_connection_error_code = None
            account.token_expires_at = expiry

            credential = store_encrypted_credential(db_session, account, token, keyring)
            credential.token_status = "valid"
            credential.issued_at = current_time
            credential.last_validated_at = current_time
            credential.last_refreshed_at = None
            credential.revoked_at = None
            credential.last_error_code = None
            credential.expires_at = expiry
            credential.scopes = scopes
            db_session.flush()
            return account
    except InstagramConnectionError:
        raise
    except (
        CredentialConfigurationError,
        CredentialEncryptionError,
        CredentialPersistenceError,
        SQLAlchemyError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        raise _connection_error() from None


def disconnect_owned_instagram_connection(
    session_factory: sessionmaker,
    *,
    user_id: object,
    account_id: object,
    legacy_instagram_user_id: object,
    now: datetime | None = None,
) -> InstagramAccount:
    """Locally disconnect one owned non-legacy account by erasing its credential."""

    normalized_user_id = _normalized_user_id(user_id)
    current_time = _utc_now(now)
    legacy_id = str(legacy_instagram_user_id or "").strip()
    try:
        with session_factory.begin() as db_session:
            account = resolve_owned_instagram_account(
                db_session, user_id=normalized_user_id, account_id=account_id
            )
            if account is None or (legacy_id and hmac.compare_digest(account.instagram_user_id, legacy_id)):
                raise _connection_error()
            credential = db_session.scalar(
                select(InstagramCredential).where(InstagramCredential.instagram_account_id == account.id)
            )
            already_disconnected = (
                account.connection_status == "disconnected"
                and account.disconnected_at is not None
                and (credential is None or (credential.token_ciphertext is None and credential.encryption_key_id is None))
            )
            if already_disconnected:
                return account
            account.connection_status = "disconnected"
            account.disconnected_at = current_time
            account.last_connection_error_code = None
            account.token_expires_at = None
            if credential is not None:
                credential.token_status = "disconnected"
                credential.token_ciphertext = None
                credential.encryption_key_id = None
                credential.scopes = None
                credential.expires_at = None
                credential.revoked_at = None
                credential.last_error_code = None
            db_session.flush()
            return account
    except InstagramConnectionError:
        raise
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        raise _connection_error() from None
