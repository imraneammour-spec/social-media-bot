"""Read-only product-level data for the internal Postenza Admin view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from database.models import InstagramAccount, User


@dataclass(frozen=True)
class AdminRecentSignup:
    display_name: str
    email: str
    created_at: datetime
    owned_instagram_accounts: int
    connected_instagram_accounts: int


@dataclass(frozen=True)
class AdminSnapshot:
    total_registered_users: int
    connected_instagram_accounts: int
    active_users: None
    recent_signups: tuple[AdminRecentSignup, ...]
    plan_distribution: None
    onboarding_source_attribution: None


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
        plan_distribution=None,
        onboarding_source_attribution=None,
    )
