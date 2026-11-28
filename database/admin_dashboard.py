"""Read-only product-level data for the internal Postenza Admin view.

User management lives here too: listing users and changing a user's status
are plain session operations (the caller owns the transaction), while
deletion is deliberately narrow — see :func:`delete_managed_user`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from database.models import (
    BillingCheckout,
    BillingSubscription,
    BillingUsage,
    InstagramAccount,
    User,
    UserOnboardingProfile,
)
from database.onboarding import LEGACY_PLAN_ALIASES, ONBOARDING_SOURCE_VALUES, PLAN_VALUES


@dataclass(frozen=True)
class AdminRecentSignup:
    display_name: str
    email: str
    created_at: datetime
    owned_instagram_accounts: int
    connected_instagram_accounts: int


@dataclass(frozen=True)
class AdminDistributionItem:
    value: str
    label: str
    count: int


@dataclass(frozen=True)
class AdminSnapshot:
    total_registered_users: int
    connected_instagram_accounts: int
    active_users: None
    recent_signups: tuple[AdminRecentSignup, ...]
    plan_distribution: tuple[AdminDistributionItem, ...]
    onboarding_source_attribution: tuple[AdminDistributionItem, ...]


PLAN_LABELS = {
    "trial": "Trial",
    "starter": "Starter",
    "pro": "Pro",
    "business": "Business",
}
ADMIN_USER_STATUSES = ("active", "disabled")
UNKNOWN_PLAN_LABEL = "Unknown / not provided"


@dataclass(frozen=True)
class AdminUserRow:
    """One row of the Admin user-management table (no credentials)."""

    user_id: uuid.UUID
    display_name: str
    email: str
    plan: str
    status: str
    created_at: datetime
    owned_instagram_accounts: int
    connected_instagram_accounts: int
    instagram_usernames: tuple[str, ...]
SOURCE_LABELS = {
    "google": "Google",
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "friend_colleague": "Friend or colleague",
    "github": "GitHub",
    "ai_tool": "AI tool or ChatGPT",
    "other": "Other",
}


def _distribution(
    session: Session,
    column,
    *,
    allowed: tuple[str, ...],
    labels: dict[str, str],
    aliases: dict[str, str] | None = None,
) -> tuple[AdminDistributionItem, ...]:
    counts = {value: 0 for value in allowed}
    unknown = 0
    rows = session.execute(
        select(column, func.count(User.id))
        .select_from(User)
        .outerjoin(UserOnboardingProfile, UserOnboardingProfile.user_id == User.id)
        .group_by(column)
    )
    for value, count in rows:
        normalized = str(value or "").strip()
        if aliases:
            normalized = aliases.get(normalized, normalized)
        if normalized in counts:
            counts[normalized] += int(count or 0)
        else:
            unknown += int(count or 0)
    return (
        *(
            AdminDistributionItem(value=value, label=labels[value], count=counts[value])
            for value in allowed
        ),
        AdminDistributionItem(
            value="unknown", label="Unknown / not provided", count=unknown
        ),
    )


def admin_snapshot(session: Session, *, recent_limit: int = 15) -> AdminSnapshot:
    """Return safe aggregates without loading credentials or operational payloads."""

    total_registered_users = int(
        session.scalar(select(func.count(User.id))) or 0
    )
    connected_instagram_accounts = int(
        session.scalar(
            select(func.count(InstagramAccount.id)).where(
                InstagramAccount.provider == "instagram",
                InstagramAccount.connection_status == "connected",
            )
        )
        or 0
    )
    recent_users = list(
        session.execute(
            select(User.id, User.display_name, User.email, User.created_at)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(recent_limit)
        )
    )
    account_counts: dict[object, tuple[int, int]] = {}
    if recent_users:
        connected_case = case(
            (
                (InstagramAccount.provider == "instagram")
                & (InstagramAccount.connection_status == "connected"),
                1,
            ),
            else_=0,
        )
        for user_id, owned_count, connected_count in session.execute(
            select(
                InstagramAccount.user_id,
                func.count(InstagramAccount.id),
                func.sum(connected_case),
            )
            .where(
                InstagramAccount.user_id.in_([user.id for user in recent_users])
            )
            .group_by(InstagramAccount.user_id)
        ):
            account_counts[user_id] = (
                int(owned_count or 0),
                int(connected_count or 0),
            )

    return AdminSnapshot(
        total_registered_users=total_registered_users,
        connected_instagram_accounts=connected_instagram_accounts,
        active_users=None,
        recent_signups=tuple(
            AdminRecentSignup(
                display_name=str(user.display_name or "Unnamed user"),
                email=str(user.email or "Unavailable"),
                created_at=user.created_at,
                owned_instagram_accounts=account_counts.get(user.id, (0, 0))[0],
                connected_instagram_accounts=account_counts.get(user.id, (0, 0))[1],
            )
            for user in recent_users
        ),
        plan_distribution=_distribution(
            session,
            UserOnboardingProfile.selected_plan,
            allowed=PLAN_VALUES,
            labels=PLAN_LABELS,
            aliases=LEGACY_PLAN_ALIASES,
        ),
        onboarding_source_attribution=_distribution(
            session,
            UserOnboardingProfile.onboarding_source,
            allowed=ONBOARDING_SOURCE_VALUES,
            labels=SOURCE_LABELS,
        ),
    )


def admin_users(session: Session, *, limit: int = 100) -> tuple[AdminUserRow, ...]:
    """List every registered user, newest first, with plan/status/accounts.

    No credentials, tokens, or operational payloads are loaded.
    """

    users = list(
        session.scalars(
            select(User)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit)
        )
    )
    if not users:
        return ()

    user_ids = [user.id for user in users]
    plans = {
        user_id: plan
        for user_id, plan in session.execute(
            select(
                UserOnboardingProfile.user_id,
                UserOnboardingProfile.selected_plan,
            ).where(UserOnboardingProfile.user_id.in_(user_ids))
        )
    }
    account_rows: dict[object, list[InstagramAccount]] = {user_id: [] for user_id in user_ids}
    for account in session.scalars(
        select(InstagramAccount)
        .where(InstagramAccount.user_id.in_(user_ids))
        .order_by(InstagramAccount.created_at, InstagramAccount.id)
    ):
        account_rows.setdefault(account.user_id, []).append(account)

    rows = []
    for user in users:
        accounts = account_rows.get(user.id, [])
        plan_value = LEGACY_PLAN_ALIASES.get(
            str(plans.get(user.id) or "").strip(),
            str(plans.get(user.id) or "").strip(),
        )
        rows.append(
            AdminUserRow(
                user_id=user.id,
                display_name=str(user.display_name or "Unnamed user"),
                email=str(user.email or "Unavailable"),
                plan=PLAN_LABELS.get(plan_value, UNKNOWN_PLAN_LABEL),
                status=str(user.status or "active"),
                created_at=user.created_at,
                owned_instagram_accounts=len(accounts),
                connected_instagram_accounts=sum(
                    1
                    for account in accounts
                    if account.provider == "instagram"
                    and account.connection_status == "connected"
                ),
                instagram_usernames=tuple(
                    str(account.username or "Instagram account")
                    for account in accounts
                ),
            )
        )
    return tuple(rows)


def set_user_status(
    session: Session,
    *,
    target_user_id: object,
    status: str,
    admin_user_id: object,
) -> User | None:
    """Set a user's status; returns the user, or None when it does not exist.

    Only ``active`` and ``disabled`` are accepted. A disabled user can no
    longer sign in: :func:`auth.get_current_user` treats any non-active
    status as signed out. The acting admin cannot change their own status.
    The caller owns the transaction (no commit here).
    """

    normalized = str(status or "").strip().lower()
    if normalized not in ADMIN_USER_STATUSES:
        raise ValueError("Choose a valid status.")
    if str(target_user_id) == str(admin_user_id):
        raise ValueError("You cannot change your own account.")
    target = session.get(User, target_user_id)
    if target is None:
        return None
    target.status = normalized
    return target


def delete_managed_user(
    session: Session,
    *,
    target_user_id: object,
    admin_user_id: object,
) -> str:
    """Delete a user when it is safe; otherwise report why it was refused.

    Returns ``"deleted"``, ``"not_found"``, ``"is_self"``,
    ``"has_instagram_accounts"``, or ``"has_billing_records"``.
    The caller owns the transaction (no commit here).

    Full cascade deletion is NOT implemented, on purpose. The ORM cascade
    from ``User`` covers only the onboarding profile and Instagram
    accounts, and the engagement tables rely on database-level
    ``ON DELETE CASCADE`` from ``instagram_accounts`` — but the billing
    tables (``billing_subscriptions``, ``billing_checkouts``,
    ``billing_usage``) reference ``users.id`` with no ``ondelete`` rule
    and no ORM relationship, so deleting a billed user would fail on
    PostgreSQL. Deletion is therefore allowed only for a user with no
    Instagram accounts and no billing records, whose only owned row is
    the onboarding profile (removed by the ORM cascade).
    """

    if str(target_user_id) == str(admin_user_id):
        return "is_self"
    target = session.get(User, target_user_id)
    if target is None:
        return "not_found"
    owned_accounts = session.scalar(
        select(func.count(InstagramAccount.id)).where(
            InstagramAccount.user_id == target.id
        )
    )
    if int(owned_accounts or 0) > 0:
        return "has_instagram_accounts"
    has_billing = bool(
        session.scalar(
            select(BillingSubscription.user_id).where(
                BillingSubscription.user_id == target.id
            )
        )
        or session.scalar(
            select(BillingCheckout.id).where(
                BillingCheckout.user_id == target.id
            )
        )
        or session.scalar(
            select(BillingUsage.user_id).where(
                BillingUsage.user_id == target.id
            )
        )
    )
    if has_billing:
        return "has_billing_records"
    session.delete(target)
    return "deleted"
