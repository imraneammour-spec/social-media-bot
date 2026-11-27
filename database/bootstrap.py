"""Explicit, idempotent bootstrap of the first future owner/account mapping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import InstagramAccount, User


class BootstrapConfigurationError(ValueError):
    """Raised when an operator has not provided safe explicit bootstrap input."""


@dataclass(frozen=True)
class BootstrapResult:
    user: User
    account: InstagramAccount
    user_created: bool
    account_created: bool


def _required(value: str | None, label: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise BootstrapConfigurationError(f"{label} must be explicitly provided.")
    return cleaned


def bootstrap_current_owner(
    session: Session,
    *,
    owner_email: str,
    owner_name: str | None,
    instagram_user_id: str,
    instagram_username: str | None = None,
) -> BootstrapResult:
    """Create the owner/account mapping once, without credentials or JSON writes.

    The caller owns the surrounding transaction.  Refusing to reassign an
    existing Instagram account prevents accidental cross-user ownership.
    """

    email = _required(owner_email, "Bootstrap owner email").lower()
    external_account_id = _required(instagram_user_id, "Instagram user ID")
    name = str(owner_name or "").strip() or None
    username = str(instagram_username or "").strip() or None

    user = session.scalar(select(User).where(User.email == email))
    user_created = user is None
    if user is None:
        # The 60-day free trial starts at creation (same as /register).
        user = User(
            email=email,
            display_name=name,
            trial_started_at=datetime.now(timezone.utc),
        )
        session.add(user)
        session.flush()

    account = session.scalar(
        select(InstagramAccount).where(
            InstagramAccount.instagram_user_id == external_account_id
        )
    )
    account_created = account is None
    if account is not None and account.user_id != user.id:
        raise BootstrapConfigurationError(
            "This Instagram account is already assigned to another user."
        )
    if account is None:
        account = InstagramAccount(
            user_id=user.id,
            instagram_user_id=external_account_id,
            username=username,
            connection_status="environment_managed",
        )
        session.add(account)
        session.flush()

    return BootstrapResult(
        user=user,
        account=account,
        user_created=user_created,
        account_created=account_created,
    )
