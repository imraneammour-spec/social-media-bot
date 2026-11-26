"""Bring-your-own Gemini API key (BYOK) storage and resolution.

One shared ``GEMINI_API_KEY`` served every user, which is what exhausted
the free quota (HTTP 429) in November 2026. With BYOK, each user can store
their own Gemini API key (Fernet-encrypted with the same keyring as the
Instagram credentials in :mod:`database.credentials`); their smart replies
and prompt extractions use their key, and the shared env key is only the
fallback when a user has none. The owner's legacy account always uses the
shared env key and is unaffected.

Key material never appears in logs or error messages. Only the last four
characters (``key_hint``) are stored for display.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .credentials import (
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialEncryptionError,
    CredentialKeyring,
    CredentialPersistenceError,
    credential_keyring_from_environment,
    decrypt_access_token,
    encrypt_access_token,
)
from .models import InstagramAccount, User, UserGeminiKey


class GeminiKeyError(ValueError):
    """Raised without key material when a BYOK operation cannot complete."""


def _keyring_or_none() -> CredentialKeyring | None:
    try:
        return credential_keyring_from_environment()
    except CredentialConfigurationError:
        return None


def keyring_available() -> bool:
    """True when the Fernet keyring is configured (BYOK storage usable)."""

    return _keyring_or_none() is not None


def store_user_gemini_key(
    session: Session,
    user: User,
    api_key: str,
    keyring: CredentialKeyring,
) -> UserGeminiKey:
    """Encrypt and store one user's Gemini API key (replaces any old one)."""

    cleaned = str(api_key or "").strip()
    if not cleaned:
        raise GeminiKeyError("A Gemini API key is required.")
    try:
        encrypted = encrypt_access_token(cleaned, keyring)
    except CredentialEncryptionError as error:
        raise GeminiKeyError("The Gemini API key could not be stored.") from error
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user.id)
        )
        now = datetime.now(timezone.utc)
        if row is None:
            row = UserGeminiKey(user_id=user.id)
            session.add(row)
        row.key_ciphertext = encrypted.token_ciphertext
        row.encryption_key_id = encrypted.encryption_key_id
        row.key_hint = cleaned[-4:]
        row.key_status = "active"
        row.last_validated_at = None
        row.last_error_code = None
        row.updated_at = now
        session.flush()
        return row
    except SQLAlchemyError as error:
        raise CredentialPersistenceError(
            "The Gemini API key could not be stored."
        ) from error


def load_user_gemini_key(
    session: Session,
    user_id: object,
    keyring: CredentialKeyring,
) -> str | None:
    """Return a user's decrypted Gemini key, or None when unusable.

    Returns None when there is no stored key, the key was marked invalid,
    or decryption fails (a rotated-away key). Never raises on bad data.
    """

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user_uuid)
        )
    except SQLAlchemyError:
        return None
    if row is None or row.key_status != "active":
        return None
    try:
        return decrypt_access_token(
            row.key_ciphertext, row.encryption_key_id, keyring
        )
    except (CredentialDecryptionError, CredentialConfigurationError):
        return None


def user_gemini_key_status(session: Session, user_id: object) -> dict[str, object]:
    """Key status for display (never decrypts, never exposes key material)."""

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return {"configured": False, "hint": None, "status": "none", "last_validated_at": None}
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user_uuid)
        )
    except SQLAlchemyError:
        return {"configured": False, "hint": None, "status": "none", "last_validated_at": None}
    if row is None or not row.key_ciphertext:
        return {"configured": False, "hint": None, "status": "none", "last_validated_at": None}
    return {
        "configured": True,
        "hint": row.key_hint,
        "status": row.key_status or "active",
        "last_validated_at": row.last_validated_at,
    }


def clear_user_gemini_key(session: Session, user_id: object) -> bool:
    """Delete a user's stored Gemini key. Returns True when one existed."""

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return False
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user_uuid)
        )
        if row is None:
            return False
        session.delete(row)
        session.flush()
        return True
    except SQLAlchemyError:
        return False


def mark_user_gemini_key_invalid(
    session: Session, user_id: object, error_code: str | None = None
) -> None:
    """Mark a stored key invalid after a live auth failure (no key material)."""

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user_uuid)
        )
        if row is None:
            return
        row.key_status = "invalid"
        row.last_error_code = str(error_code or "")[:128] or None
        session.flush()
    except SQLAlchemyError:
        return


def mark_user_gemini_key_validated(session: Session, user_id: object) -> None:
    """Record a successful live validation of the stored key."""

    try:
        user_uuid = uuid.UUID(str(user_id))
    except (TypeError, ValueError, AttributeError):
        return
    try:
        row = session.scalar(
            select(UserGeminiKey).where(UserGeminiKey.user_id == user_uuid)
        )
        if row is None:
            return
        row.key_status = "active"
        row.last_validated_at = datetime.now(timezone.utc)
        row.last_error_code = None
        session.flush()
    except SQLAlchemyError:
        return


def resolve_account_gemini_key(
    session_factory: object,
    account_id: object,
    keyring: CredentialKeyring | None = None,
) -> str | None:
    """Return the BYOK key for an Instagram account's owner, or None.

    Falls back to None (caller uses the shared env key) when the keyring
    is unconfigured, the account has no owner, or the owner has no usable
    stored key.
    """

    if session_factory is None:
        return None
    try:
        account_uuid = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return None
    active_keyring = keyring if keyring is not None else _keyring_or_none()
    if active_keyring is None:
        return None
    try:
        with session_factory() as session:  # type: ignore[operator]
            account = session.get(InstagramAccount, account_uuid)
            if account is None:
                return None
            return load_user_gemini_key(session, account.user_id, active_keyring)
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        return None


def validate_gemini_api_key(api_key: str, *, client_factory: object = None) -> str:
    """Live-check an API key. Returns "valid", "invalid", or "unknown".

    "unknown" means the check could not reach Google (network/transient
    error) — the key itself may still be fine. ``client_factory`` is a
    test seam: ``client_factory(api_key)`` must return an object with a
    ``models.list(page_size=1)`` method.
    """

    cleaned = str(api_key or "").strip()
    if not cleaned:
        return "invalid"
    try:
        if client_factory is not None:
            client = client_factory(cleaned)  # type: ignore[operator]
        else:
            from google import genai

            client = genai.Client(api_key=cleaned)
        models = client.models.list(page_size=1)
        next(iter(models), None)
        return "valid"
    except Exception as error:  # noqa: BLE001 - mapped to a safe status
        code = getattr(error, "code", None)
        detail = str(error or "")
        if code in (400, 401, 403) or "API_KEY_INVALID" in detail or "API key not valid" in detail:
            return "invalid"
        return "unknown"
