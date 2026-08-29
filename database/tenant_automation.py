"""Tenant-owned OAuth automation configuration defaults and rollout helpers."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import InstagramAccount, InstagramAutomationConfig, InstagramCredential
from .oauth_capability import oauth_engagement_prerequisites_met


CURRENT_DEFAULT_TRIGGER_KEYWORDS = (
    "access",
    "ai",
    "prompt",
    "prompts",
    "send",
)
CURRENT_DEFAULT_PUBLIC_REPLY_TEXT = "Your prompts are ready. Check your messages."
_KEYWORD_PATTERN = re.compile(r"^\w{1,32}$")


class TenantAutomationConfigurationError(ValueError):
    """Raised when tenant automation configuration values are malformed."""


def normalize_trigger_keywords(value: object) -> list[str]:
    """Normalize a bounded list of single tokens for future OAuth matching."""

    if not isinstance(value, list) or not (1 <= len(value) <= 10):
        raise TenantAutomationConfigurationError("Invalid automation trigger keywords.")
    normalized: list[str] = []
    for item in value:
        keyword = item.strip().lower() if isinstance(item, str) else ""
        if not _KEYWORD_PATTERN.fullmatch(keyword):
            raise TenantAutomationConfigurationError("Invalid automation trigger keywords.")
        if keyword not in normalized:
            normalized.append(keyword)
    if not 1 <= len(normalized) <= 10:
        raise TenantAutomationConfigurationError("Invalid automation trigger keywords.")
    return normalized


def current_default_trigger_keywords() -> list[str]:
    """Return a fresh normalized copy of the existing OAuth trigger set."""

    return list(CURRENT_DEFAULT_TRIGGER_KEYWORDS)


def current_default_public_reply_text() -> str:
    """Return the exact public reply currently used by OAuth automation."""

    return CURRENT_DEFAULT_PUBLIC_REPLY_TEXT


def initial_automation_enabled(
    account: InstagramAccount | None,
    credential: InstagramCredential | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Determine tenant default eligibility without consulting global environment."""

    return oauth_engagement_prerequisites_met(account, credential, now=now)


def ensure_initial_oauth_automation_config(
    session: Session,
    *,
    account: InstagramAccount,
    credential: InstagramCredential | None,
    now: datetime | None = None,
) -> InstagramAutomationConfig | None:
    """Create one initial config without overwriting an existing tenant choice."""

    if not _is_oauth_managed_account(account):
        return None
    existing = session.scalar(select(InstagramAutomationConfig).where(
        InstagramAutomationConfig.instagram_account_id == account.id
    ))
    if existing is not None:
        return existing

    config = InstagramAutomationConfig(
        instagram_account_id=account.id,
        enabled=initial_automation_enabled(account, credential, now=now),
        trigger_keywords=current_default_trigger_keywords(),
        public_reply_enabled=True,
        public_reply_text=current_default_public_reply_text(),
    )
    try:
        with session.begin_nested():
            session.add(config)
            session.flush()
        return config
    except IntegrityError:
        return session.scalar(select(InstagramAutomationConfig).where(
            InstagramAutomationConfig.instagram_account_id == account.id
        ))


def backfill_oauth_automation_configs(
    session: Session, *, now: datetime | None = None
) -> int:
    """Backfill OAuth-managed accounts without changing existing tenant rows."""

    created = 0
    accounts = session.scalars(select(InstagramAccount).where(
        InstagramAccount.provider == "instagram",
        InstagramAccount.connection_status != "environment_managed",
    )).all()
    for account in accounts:
        existing = session.scalar(select(InstagramAutomationConfig).where(
            InstagramAutomationConfig.instagram_account_id == account.id
        ))
        if existing is not None:
            continue
        credential = session.scalar(select(InstagramCredential).where(
            InstagramCredential.instagram_account_id == account.id
        ))
        config = ensure_initial_oauth_automation_config(
            session, account=account, credential=credential, now=now
        )
        if config is not None:
            created += 1
    return created


def _is_oauth_managed_account(account: InstagramAccount | None) -> bool:
    return bool(
        account is not None
        and account.provider == "instagram"
        and account.connection_status != "environment_managed"
    )
