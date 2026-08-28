"""Server-only, fail-closed per-account OAuth webhook subscription support."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import requests
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .account_runtime import INSTAGRAM_EXTERNAL_ID_PATTERN
from .credentials import (
    CredentialConfigurationError,
    CredentialDecryptionError,
    CredentialKeyring,
    CredentialNotFoundError,
    credential_keyring_from_environment,
    decrypt_access_token,
)
from .models import InstagramAccount, InstagramCredential
from .oauth_capability import REQUIRED_OAUTH_ENGAGEMENT_SCOPES
from .oauth_graph import API_VERSION, GRAPH_ROOT


OAUTH_SUBSCRIPTION_PENDING_STATUS = "subscription_pending"
OAUTH_SUBSCRIBED_FIELDS = ("comments", "messages")
OAUTH_SUBSCRIBED_FIELDS_PARAMETER = ",".join(OAUTH_SUBSCRIBED_FIELDS)
OAUTH_SUBSCRIPTION_TIMEOUT_SECONDS = 15
_EXTERNAL_ID_PATTERN = re.compile(INSTAGRAM_EXTERNAL_ID_PATTERN.pattern)
WEBHOOK_SUBSCRIPTION_PRECONDITION_FAILED = "webhook_subscription_precondition_failed"
WEBHOOK_SUBSCRIPTION_CREDENTIAL_UNAVAILABLE = "webhook_subscription_credential_unavailable"
WEBHOOK_SUBSCRIPTION_TRANSPORT_FAILED = "webhook_subscription_transport_failed"
WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED = "webhook_subscription_provider_rejected"
WEBHOOK_SUBSCRIPTION_PROVIDER_RESPONSE_INVALID = "webhook_subscription_provider_response_invalid"
_FAILURE_CODES = frozenset((
    WEBHOOK_SUBSCRIPTION_PRECONDITION_FAILED,
    WEBHOOK_SUBSCRIPTION_CREDENTIAL_UNAVAILABLE,
    WEBHOOK_SUBSCRIPTION_TRANSPORT_FAILED,
    WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED,
    WEBHOOK_SUBSCRIPTION_PROVIDER_RESPONSE_INVALID,
))
_GENERIC_SUBSCRIPTION_MESSAGE = "Instagram webhook subscription is unavailable."


class OAuthWebhookSubscriptionError(ValueError):
    """Credential-safe failure for one account webhook subscription attempt."""

    def __init__(self, failure_code: str):
        if failure_code not in _FAILURE_CODES:
            raise ValueError("Invalid webhook subscription failure code.")
        self.failure_code = failure_code
        super().__init__(_GENERIC_SUBSCRIPTION_MESSAGE)


def _unavailable(
    failure_code: str = WEBHOOK_SUBSCRIPTION_PRECONDITION_FAILED,
) -> OAuthWebhookSubscriptionError:
    return OAuthWebhookSubscriptionError(failure_code)


def _normalized_account_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _unavailable() from None


def _normalized_external_id(value: object) -> str:
    external_id = value.strip() if isinstance(value, str) else ""
    if not _EXTERNAL_ID_PATTERN.fullmatch(external_id):
        raise _unavailable()
    return external_id


def _utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _is_expired(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return current <= now


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _granted_scopes(value: object) -> set[str]:
    return {scope for scope in value if isinstance(scope, str)} if isinstance(value, list) else set()


def _usable_pending_subscription(
    account: InstagramAccount | None,
    credential: InstagramCredential | None,
    *,
    now: datetime,
) -> bool:
    return bool(
        account is not None
        and credential is not None
        and account.provider == "instagram"
        and account.connection_status == OAUTH_SUBSCRIPTION_PENDING_STATUS
        and not _text(account.last_connection_error_code)
        and not _is_expired(account.token_expires_at, now)
        and credential.token_status == "valid"
        and credential.revoked_at is None
        and not _text(credential.last_error_code)
        and _text(credential.token_ciphertext)
        and _text(credential.encryption_key_id)
        and not _is_expired(credential.expires_at, now)
        and REQUIRED_OAUTH_ENGAGEMENT_SCOPES.issubset(_granted_scopes(credential.scopes))
    )


def subscribe_oauth_account_webhooks(
    session: Session,
    *,
    instagram_account_id: object,
    instagram_user_id: object,
    keyring: CredentialKeyring | None = None,
    now: datetime | None = None,
) -> bool:
    """Subscribe one staged OAuth account to exactly comments and messages.

    The caller must hold the transaction that staged the replacement credential.
    This function never reads global credentials, calls legacy helpers, logs,
    persists provider responses, or dispatches engagement operations.
    """

    account_id = _normalized_account_id(instagram_account_id)
    external_id = _normalized_external_id(instagram_user_id)
    current = _utc_now(now)
    try:
        account = session.scalar(select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.instagram_user_id == external_id,
        ))
        credential = session.scalar(select(InstagramCredential).where(
            InstagramCredential.instagram_account_id == account_id,
        )) if account is not None else None
        if not _usable_pending_subscription(account, credential, now=current):
            raise _unavailable()
        try:
            active_keyring = keyring if keyring is not None else credential_keyring_from_environment()
            access_token = decrypt_access_token(
                credential.token_ciphertext,
                credential.encryption_key_id,
                active_keyring,
            )
        except (
            CredentialConfigurationError,
            CredentialDecryptionError,
            CredentialNotFoundError,
            AttributeError,
            TypeError,
            ValueError,
        ):
            raise _unavailable(WEBHOOK_SUBSCRIPTION_CREDENTIAL_UNAVAILABLE) from None
        if not access_token:
            raise _unavailable(WEBHOOK_SUBSCRIPTION_CREDENTIAL_UNAVAILABLE)
        try:
            response = requests.post(
                f"{GRAPH_ROOT}/{API_VERSION}/{external_id}/subscribed_apps",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"subscribed_fields": OAUTH_SUBSCRIBED_FIELDS_PARAMETER},
                timeout=OAUTH_SUBSCRIPTION_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            raise _unavailable(WEBHOOK_SUBSCRIPTION_TRANSPORT_FAILED) from None
        try:
            if not response.ok:
                raise _unavailable(WEBHOOK_SUBSCRIPTION_PROVIDER_REJECTED)
            payload = response.json()
        except OAuthWebhookSubscriptionError:
            raise
        except (AttributeError, TypeError, ValueError):
            raise _unavailable(WEBHOOK_SUBSCRIPTION_PROVIDER_RESPONSE_INVALID) from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise _unavailable(WEBHOOK_SUBSCRIPTION_PROVIDER_RESPONSE_INVALID)
        return True
    except OAuthWebhookSubscriptionError:
        raise
    except (
        SQLAlchemyError,
    ):
        raise _unavailable() from None
