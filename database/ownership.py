"""Server-side ownership resolution for Instagram account boundaries."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import InstagramAccount


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def resolve_owned_instagram_account(
    session: Session,
    *,
    user_id: object,
    account_id: object,
) -> InstagramAccount | None:
    """Return an account only when its UUID belongs to the supplied user.

    Invalid, nonexistent, and non-owned identifiers intentionally all return
    ``None`` so callers can use one externally safe failure response.
    """

    normalized_user_id = _as_uuid(user_id)
    normalized_account_id = _as_uuid(account_id)
    if normalized_user_id is None or normalized_account_id is None:
        return None
    return session.scalar(
        select(InstagramAccount).where(
            InstagramAccount.id == normalized_account_id,
            InstagramAccount.user_id == normalized_user_id,
        )
    )


def resolve_owned_instagram_account_by_external_id(
    session: Session,
    *,
    user_id: object,
    instagram_user_id: object,
) -> InstagramAccount | None:
    """Return a user's account only when it owns the external Instagram ID."""

    normalized_user_id = _as_uuid(user_id)
    external_id = str(instagram_user_id or "").strip()
    if normalized_user_id is None or not external_id:
        return None
    return session.scalar(
        select(InstagramAccount).where(
            InstagramAccount.user_id == normalized_user_id,
            InstagramAccount.instagram_user_id == external_id,
        )
    )
