"""Tenant-owned OAuth automation configuration defaults and rollout helpers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, JSON, Text, Uuid, column, select, table
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
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
DEFAULT_SMART_REPLY_TONE = "friendly"
DEFAULT_SMART_REPLY_MAX_PER_HOUR = 20
SMART_REPLY_TONES = ("friendly", "professional", "concise", "neutral")
SMART_REPLY_MIN_PER_HOUR = 1
SMART_REPLY_MAX_PER_HOUR = 100
_KEYWORD_PATTERN = re.compile(r"^\w{1,32}$")


class TenantAutomationConfigurationError(ValueError):
    """Raised when tenant automation configuration values are malformed."""


@dataclass(frozen=True)
class OAuthAutomationRuntimeConfig:
    """Validated, non-secret automation choices for one OAuth account."""

    enabled: bool
    trigger_keywords: tuple[str, ...]
    public_reply_enabled: bool
    public_reply_text: str
    smart_reply_enabled: bool
    smart_reply_tone: str
    smart_reply_max_per_hour: int


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


def normalize_smart_reply_tone(value: object) -> str:
    """Return one finite public-reply tone, rejecting all other input."""

    tone = str(value or "").strip().lower()
    if tone not in SMART_REPLY_TONES:
        raise TenantAutomationConfigurationError("Invalid smart reply tone.")
    return tone


def normalize_smart_reply_max_per_hour(value: object) -> int:
    """Return a bounded per-account smart-reply limit."""

    if isinstance(value, bool):
        raise TenantAutomationConfigurationError("Invalid smart reply limit.")
    try:
        limit = int(str(value).strip())
    except (TypeError, ValueError):
        raise TenantAutomationConfigurationError("Invalid smart reply limit.") from None
    if not SMART_REPLY_MIN_PER_HOUR <= limit <= SMART_REPLY_MAX_PER_HOUR:
        raise TenantAutomationConfigurationError("Invalid smart reply limit.")
    return limit


def oauth_automation_runtime_config(
    session: Session,
    *,
    instagram_account_id: object,
) -> OAuthAutomationRuntimeConfig | None:
    """Return one account's valid automation settings, or nothing."""

    try:
        account_id = uuid.UUID(str(instagram_account_id))
        config = session.scalar(select(InstagramAutomationConfig).where(
            InstagramAutomationConfig.instagram_account_id == account_id
        ))
        if config is None:
            return None
        keywords = tuple(normalize_trigger_keywords(config.trigger_keywords))
        enabled = config.enabled
        public_reply_enabled = config.public_reply_enabled
        public_reply_text = config.public_reply_text
        smart_reply_enabled = config.smart_reply_enabled
        smart_reply_tone = normalize_smart_reply_tone(config.smart_reply_tone)
        smart_reply_max_per_hour = normalize_smart_reply_max_per_hour(
            config.smart_reply_max_per_hour
        )
        if (
            not isinstance(enabled, bool)
            or not isinstance(public_reply_enabled, bool)
            or not isinstance(public_reply_text, str)
            or not isinstance(smart_reply_enabled, bool)
        ):
            return None
        public_reply_text = public_reply_text.strip()
        if public_reply_enabled and not public_reply_text:
            return None
        return OAuthAutomationRuntimeConfig(
            enabled=enabled,
            trigger_keywords=keywords,
            public_reply_enabled=public_reply_enabled,
            public_reply_text=public_reply_text,
            smart_reply_enabled=smart_reply_enabled,
            smart_reply_tone=smart_reply_tone,
            smart_reply_max_per_hour=smart_reply_max_per_hour,
        )
    except (
        TenantAutomationConfigurationError,
        SQLAlchemyError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        return None


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
        smart_reply_enabled=False,
        smart_reply_tone=DEFAULT_SMART_REPLY_TONE,
        smart_reply_max_per_hour=DEFAULT_SMART_REPLY_MAX_PER_HOUR,
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

    config_table = table(
        "instagram_automation_configs",
        column("id", Uuid()),
        column("instagram_account_id", Uuid()),
        column("enabled", Boolean()),
        column("trigger_keywords", JSON()),
        column("public_reply_enabled", Boolean()),
        column("public_reply_text", Text()),
    )
    created = 0
    accounts = session.scalars(select(InstagramAccount).where(
        InstagramAccount.provider == "instagram",
        InstagramAccount.connection_status != "environment_managed",
    )).all()
    for account in accounts:
        existing_account_id = session.scalar(select(
            config_table.c.instagram_account_id
        ).where(config_table.c.instagram_account_id == account.id))
        if existing_account_id is not None:
            continue
        credential = session.scalar(select(InstagramCredential).where(
            InstagramCredential.instagram_account_id == account.id
        ))
        with session.begin_nested():
            session.execute(config_table.insert().values(
                id=uuid.uuid4(),
                instagram_account_id=account.id,
                enabled=initial_automation_enabled(account, credential, now=now),
                trigger_keywords=current_default_trigger_keywords(),
                public_reply_enabled=True,
                public_reply_text=current_default_public_reply_text(),
            ))
        created += 1
    return created


def _is_oauth_managed_account(account: InstagramAccount | None) -> bool:
    return bool(
        account is not None
        and account.provider == "instagram"
        and account.connection_status != "environment_managed"
    )
