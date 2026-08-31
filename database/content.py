"""Minimal, account-scoped content foundation for Drafts and ready-to-publish items."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import InstagramContentItem

CONTENT_TYPES = ("image", "video", "carousel", "reel")
VALID_STATUSES = {"draft", "ready"}
MAX_CAPTION_LENGTH = 2200


def normalize_content_type(value: object) -> str:
    """Normalize and validate a supported content type."""

    if value is None:
        raise ValueError("Content type is required.")
    normalized = str(value).strip().lower()
    if normalized not in CONTENT_TYPES:
        raise ValueError("Unsupported content type.")
    return normalized


def _normalize_status(value: object) -> str:
    if value is None:
        raise ValueError("Status is required.")
    normalized = str(value).strip().lower()
    if normalized not in VALID_STATUSES:
        raise ValueError("Unsupported content status.")
    return normalized


def _validate_media_url(value: object) -> str:
    if value is None:
        raise ValueError("Media URL is required.")
    url = str(value).strip()
    if not url:
        raise ValueError("Media URL is required.")
    if len(url) > 2048:
        raise ValueError("Media URL is too long.")
    try:
        parsed = urlsplit(url)
    except ValueError as exc:  # pragma: no cover - impossible with a string
        raise ValueError("Media URL is invalid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Media URL must use http or https.")
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in {"localhost", "127.0.0.1"}:
        raise ValueError("Media URL must point to a remote host.")
    return url


def _load_owned_item(session: Session, instagram_account_id: object, item_id: object) -> InstagramContentItem:
    account_id = uuid.UUID(str(instagram_account_id))
    item = session.scalar(
        select(InstagramContentItem).where(
            InstagramContentItem.instagram_account_id == account_id,
            InstagramContentItem.id == uuid.UUID(str(item_id)),
        )
    )
    if item is None:
        raise PermissionError("Content item not found for this account.")
    return item


def create_content_draft(
    session: Session,
    *,
    instagram_account_id: object,
    caption: str,
    content_type: object,
    media_url: object,
    status: object = "draft",
    **kwargs,
) -> InstagramContentItem:
    """Create a durably scoped draft for a single Instagram account."""

    account_id = uuid.UUID(str(instagram_account_id))
    normalized_status = _normalize_status(status)
    normalized_type = normalize_content_type(content_type)
    validated_url = _validate_media_url(media_url)
    trimmed_caption = str(caption or "").strip()
    if not trimmed_caption:
        raise ValueError("Caption is required.")
    if len(trimmed_caption) > MAX_CAPTION_LENGTH:
        raise ValueError("Caption is too long.")

    item = InstagramContentItem(
        instagram_account_id=account_id,
        status=normalized_status,
        content_type=normalized_type,
        caption=trimmed_caption,
        media_url=validated_url,
        cover_url=kwargs.get("cover_url") or validated_url,
        alt_text=kwargs.get("alt_text"),
        metadata_json=kwargs.get("metadata_json") or {"content_type": normalized_type},
    )
    session.add(item)
    session.flush()
    return item


def list_content_drafts(session: Session, instagram_account_id: object) -> list[InstagramContentItem]:
    """Return all draft-ready content rows for one owned account."""

    account_id = uuid.UUID(str(instagram_account_id))
    return list(
        session.scalars(
            select(InstagramContentItem)
            .where(InstagramContentItem.instagram_account_id == account_id)
            .order_by(InstagramContentItem.created_at.desc(), InstagramContentItem.id.desc())
        )
    )


def update_content_draft(
    session: Session,
    instagram_account_id: object,
    item_id: object,
    **changes,
) -> InstagramContentItem:
    """Update a content draft only when it belongs to the provided account."""

    item = _load_owned_item(session, instagram_account_id, item_id)
    if "caption" in changes:
        raw_caption = changes["caption"]
        value = str(raw_caption or "").strip()
        if not value:
            raise ValueError("Caption is required.")
        if len(value) > MAX_CAPTION_LENGTH:
            raise ValueError("Caption is too long.")
        item.caption = value
    if "content_type" in changes:
        item.content_type = normalize_content_type(changes["content_type"])
    if "media_url" in changes:
        item.media_url = _validate_media_url(changes["media_url"])
        if not item.cover_url:
            item.cover_url = item.media_url
    if "cover_url" in changes:
        item.cover_url = _validate_media_url(changes["cover_url"]) if changes["cover_url"] else None
    if "alt_text" in changes:
        item.alt_text = changes["alt_text"]
    if "status" in changes:
        item.status = _normalize_status(changes["status"])
    if "metadata_json" in changes:
        item.metadata_json = changes["metadata_json"]
    session.flush()
    return item


def transition_content_status(
    session: Session,
    instagram_account_id: object,
    item_id: object,
    new_status: object,
) -> InstagramContentItem:
    """Only allow the two supported Phase 7A transitions: draft->ready and ready->draft."""

    item = _load_owned_item(session, instagram_account_id, item_id)
    desired = _normalize_status(new_status)
    if desired not in VALID_STATUSES:
        raise ValueError("Unsupported content status.")
    allowed = {
        "draft": {"ready"},
        "ready": {"draft"},
    }
    if desired not in allowed.get(item.status, set()):
        raise ValueError("Invalid status transition.")
    item.status = desired
    session.flush()
    return item


def mark_content_ready(session: Session, instagram_account_id: object, item_id: object) -> InstagramContentItem:
    """Move an owned draft into the ready state for later publishing."""

    return transition_content_status(session, instagram_account_id, item_id, "ready")


def delete_content_draft(session: Session, instagram_account_id: object, item_id: object) -> None:
    """Delete a single content item only for the owning account."""

    item = _load_owned_item(session, instagram_account_id, item_id)
    session.delete(item)
    session.flush()


__all__ = [
    "CONTENT_TYPES",
    "MAX_CAPTION_LENGTH",
    "create_content_draft",
    "delete_content_draft",
    "list_content_drafts",
    "mark_content_ready",
    "normalize_content_type",
    "update_content_draft",
]
