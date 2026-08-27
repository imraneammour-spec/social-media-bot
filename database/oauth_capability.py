"""Fail-closed, token-free OAuth engagement capability assessment."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import InstagramAccount, InstagramCredential


INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED_ENV = "INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED"
REQUIRED_OAUTH_ENGAGEMENT_SCOPES = frozenset({
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
})
_INSTAGRAM_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")


@dataclass(frozen=True)
class OAuthEngagementCapability:
    """A credential-safe readiness result with a deliberately finite state."""

    state: str

    @property
    def is_active(self) -> bool:
        return self.state == "active"


def oauth_engagement_enabled_from_environment(
    environment: Mapping[str, object] | None = None,
) -> bool:
    """Return true only for the explicit, approved enable value."""

    source = os.environ if environment is None else environment
    value = source.get(INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED_ENV)
    return isinstance(value, str) and value.strip().lower() == "true"


def assess_oauth_engagement_capability(
    session: Session,
    *,
    instagram_account_id: object,
    instagram_user_id: object,
    now: datetime | None = None,
    environment: Mapping[str, object] | None = None,
) -> OAuthEngagementCapability:
    """Assess one exact OAuth account without decrypting its credential."""

    try:
        account_id = uuid.UUID(str(instagram_account_id))
        external_id = _normalized_external_id(instagram_user_id)
        if external_id is None:
            return _reconnect_required()
        account = session.scalar(select(InstagramAccount).where(
            InstagramAccount.id == account_id,
            InstagramAccount.instagram_user_id == external_id,
        ))
        credential = session.scalar(select(InstagramCredential).where(
            InstagramCredential.instagram_account_id == account_id,
        )) if account is not None else None
        return assess_oauth_engagement_records(
            account,
            credential,
            now=now,
            environment=environment,
        )
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        return OAuthEngagementCapability("temporarily_unavailable")


def assess_oauth_engagement_records(
    account: InstagramAccount | None,
    credential: InstagramCredential | None,
    *,
    now: datetime | None = None,
    environment: Mapping[str, object] | None = None,
) -> OAuthEngagementCapability:
    """Assess already-loaded account records for non-secret presentation/use."""

    current_time = _normalized_now(now)
    if not _engagement_prerequisites_met(account, credential, now=current_time):
        return _reconnect_required()
    if not oauth_engagement_enabled_from_environment(environment):
        return OAuthEngagementCapability("ready_for_activation")
    return OAuthEngagementCapability("active")


def _engagement_prerequisites_met(
    account: InstagramAccount | None,
    credential: InstagramCredential | None,
    *,
    now: datetime,
) -> bool:
    if account is None or credential is None:
        return False
    granted = _granted_scopes(credential.scopes)
    return bool(
        account.provider == "instagram"
        and account.connection_status == "connected"
        and not _text(account.last_connection_error_code)
        and not _is_expired(account.token_expires_at, now)
        and credential.token_status == "valid"
        and credential.revoked_at is None
        and not _text(credential.last_error_code)
        and _text(credential.token_ciphertext)
        and _text(credential.encryption_key_id)
        and not _is_expired(credential.expires_at, now)
        and REQUIRED_OAUTH_ENGAGEMENT_SCOPES.issubset(granted)
    )


def _granted_scopes(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _normalized_external_id(value: object) -> str | None:
    external_id = value.strip() if isinstance(value, str) else ""
    return external_id if _INSTAGRAM_EXTERNAL_ID_PATTERN.fullmatch(external_id) else None


def _normalized_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _is_expired(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return False
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return current <= now


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _reconnect_required() -> OAuthEngagementCapability:
    return OAuthEngagementCapability("reconnect_required")
