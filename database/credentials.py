"""Server-only encrypted storage for future per-account Instagram credentials."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import InstagramAccount, InstagramCredential


ACTIVE_KEY_ID_ENV = "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID"
KEY_ENV_PREFIX = "INSTAGRAM_CREDENTIAL_KEY_"
KEY_ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class CredentialConfigurationError(ValueError):
    """Raised without secret material when credential key configuration is invalid."""


class CredentialEncryptionError(ValueError):
    """Raised without plaintext when a credential cannot be encrypted."""


class CredentialDecryptionError(ValueError):
    """Raised without ciphertext or key material when decryption fails."""


class CredentialNotFoundError(ValueError):
    """Raised when a trusted server operation has no stored credential."""


class CredentialPersistenceError(ValueError):
    """Raised without credential material when encrypted persistence fails."""


@dataclass(frozen=True)
class EncryptedAccessToken:
    """Encrypted credential data suitable for the existing database columns."""

    token_ciphertext: str
    encryption_key_id: str


@dataclass(frozen=True)
class CredentialKeyring:
    """Active Fernet key plus retained decrypt-only keys, all server-side."""

    active_key_id: str
    _keys: Mapping[str, Fernet]

    def active_key(self) -> Fernet:
        return self._keys[self.active_key_id]

    def key_for(self, key_id: str) -> Fernet | None:
        return self._keys.get(key_id)


def _validated_key_id(value: object) -> str:
    key_id = value if isinstance(value, str) else ""
    if not KEY_ID_PATTERN.fullmatch(key_id):
        raise CredentialConfigurationError("Credential encryption key configuration is invalid.")
    return key_id


def credential_keyring_from_environment(
    environ: Mapping[str, str] | None = None,
) -> CredentialKeyring:
    """Load an active key and safely named retained keys without logging them."""

    source = os.environ if environ is None else environ
    active_key_id = _validated_key_id(source.get(ACTIVE_KEY_ID_ENV, ""))
    keys: dict[str, Fernet] = {}

    for environment_name, key_value in source.items():
        if not environment_name.startswith(KEY_ENV_PREFIX):
            continue
        key_id = _validated_key_id(environment_name.removeprefix(KEY_ENV_PREFIX))
        try:
            keys[key_id] = Fernet(str(key_value).encode("ascii"))
        except (TypeError, UnicodeEncodeError, ValueError):
            raise CredentialConfigurationError(
                "Credential encryption key configuration is invalid."
            ) from None

    if active_key_id not in keys:
        raise CredentialConfigurationError("Credential encryption key configuration is invalid.")
    return CredentialKeyring(active_key_id=active_key_id, _keys=keys)


def encrypt_access_token(access_token: str, keyring: CredentialKeyring) -> EncryptedAccessToken:
    """Encrypt a nonempty token with the configured active Fernet key."""

    if not isinstance(access_token, str) or not access_token:
        raise CredentialEncryptionError("Credential encryption failed.")
    try:
        ciphertext = keyring.active_key().encrypt(access_token.encode("utf-8")).decode("ascii")
    except (AttributeError, TypeError, UnicodeError, ValueError):
        raise CredentialEncryptionError("Credential encryption failed.") from None
    return EncryptedAccessToken(
        token_ciphertext=ciphertext,
        encryption_key_id=keyring.active_key_id,
    )


def decrypt_access_token(
    token_ciphertext: str | None,
    encryption_key_id: str | None,
    keyring: CredentialKeyring,
) -> str:
    """Decrypt only for a trusted server-side operation with a retained key."""

    try:
        key_id = _validated_key_id(encryption_key_id)
        fernet = keyring.key_for(key_id)
        if fernet is None or not isinstance(token_ciphertext, str) or not token_ciphertext:
            raise CredentialDecryptionError("Credential decryption failed.")
        return fernet.decrypt(token_ciphertext.encode("ascii")).decode("utf-8")
    except CredentialDecryptionError:
        raise
    except (
        CredentialConfigurationError,
        AttributeError,
        TypeError,
        UnicodeError,
        ValueError,
        InvalidToken,
    ):
        raise CredentialDecryptionError("Credential decryption failed.") from None


def store_encrypted_credential(
    session: Session,
    account: InstagramAccount,
    access_token: str,
    keyring: CredentialKeyring,
) -> InstagramCredential:
    """Explicitly create or replace one account credential with ciphertext only."""

    encrypted = encrypt_access_token(access_token, keyring)
    try:
        credential = session.scalar(
            select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == account.id
            )
        )
        if credential is None:
            credential = InstagramCredential(instagram_account_id=account.id)
            session.add(credential)
        credential.token_ciphertext = encrypted.token_ciphertext
        credential.encryption_key_id = encrypted.encryption_key_id
        session.flush()
        return credential
    except SQLAlchemyError:
        raise CredentialPersistenceError("Encrypted credential storage failed.") from None


def load_decrypted_access_token(
    session: Session,
    account: InstagramAccount,
    keyring: CredentialKeyring,
) -> str:
    """Return plaintext only to the immediate trusted server-side caller."""

    try:
        credential = session.scalar(
            select(InstagramCredential).where(
                InstagramCredential.instagram_account_id == account.id
            )
        )
    except SQLAlchemyError:
        raise CredentialNotFoundError("Credential is unavailable.") from None
    if credential is None:
        raise CredentialNotFoundError("Credential is unavailable.")
    return decrypt_access_token(
        credential.token_ciphertext,
        credential.encryption_key_id,
        keyring,
    )
