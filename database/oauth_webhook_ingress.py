"""Durable, account-scoped OAuth webhook ingress work coordination."""

from __future__ import annotations

import secrets
import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import InstagramAccount, InstagramOAuthWebhookWorkItem


WEBHOOK_WORK_LEASE_DURATION = timedelta(minutes=2)


@dataclass(frozen=True)
class ClaimedOAuthWebhookWork:
    id: uuid.UUID
    instagram_account_id: uuid.UUID
    instagram_user_id: str
    provider_event_id: str
    event_type: str
    payload: dict[str, object]
    public_base_url: str
    lease_token: str


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _text(value: object, *, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _public_base_url(value: object) -> str | None:
    candidate = _text(value, limit=2048)
    try:
        parsed = urlsplit(candidate)
        local_http = parsed.scheme == "http" and (parsed.hostname or "").lower() in {
            "localhost", "127.0.0.1", "::1",
        }
        if (
            (parsed.scheme != "https" and not local_http)
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return None
    except (TypeError, ValueError):
        return None
    return candidate.rstrip("/")


def _work_payload(event_type: object, value: object) -> tuple[str, str, dict[str, object]] | None:
    """Retain only the fields the existing OAuth processors consume."""
    if not isinstance(value, dict):
        return None
    if event_type == "comment":
        provider_event_id = _text(value.get("id"), limit=255)
        comment_text = _text(value.get("text"), limit=4096)
        commenter = value.get("from") if isinstance(value.get("from"), dict) else {}
        commenter_id = _text(commenter.get("id"), limit=255)
        username = _text(commenter.get("username"), limit=255)
        media = value.get("media") if isinstance(value.get("media"), dict) else {}
        media_id = _text(media.get("id"), limit=255)
        if not provider_event_id or not comment_text or not commenter_id:
            return None
        return (
            "comment",
            provider_event_id,
            {
                "id": provider_event_id,
                "text": comment_text,
                "from": {"id": commenter_id, "username": username},
                "media": {"id": media_id},
            },
        )
    if event_type == "message":
        message = value.get("message") if isinstance(value.get("message"), dict) else {}
        sender = value.get("sender") if isinstance(value.get("sender"), dict) else {}
        provider_event_id = _text(message.get("mid"), limit=255)
        sender_id = _text(sender.get("id"), limit=255)
        if not provider_event_id or not sender_id:
            return None
        quick = message.get("quick_reply") if isinstance(message.get("quick_reply"), dict) else {}
        recipient = value.get("recipient") if isinstance(value.get("recipient"), dict) else {}
        return (
            "message",
            provider_event_id,
            {
                "sender": {"id": sender_id},
                "is_non_reply": bool(value.get("read") or value.get("delivery") or message.get("is_deleted")),
                "timestamp": (
                    value.get("timestamp")
                    if type(value.get("timestamp")) in (int, float) else None
                ),
                "recipient": {"id": _text(recipient.get("id"), limit=255)},
                "message": {
                    "mid": provider_event_id,
                    "text": _text(message.get("text"), limit=4096),
                    "is_echo": bool(message.get("is_echo")),
                    "has_attachments": bool(message.get("attachments")),
                    "quick_reply": {"payload": _text(quick.get("payload"), limit=255)},
                },
            },
        )
    return None


def enqueue_oauth_webhook_work(
    session: Session,
    *,
    instagram_account_id: object,
    instagram_user_id: object,
    event_type: object,
    value: object,
    public_base_url: object = "https://public.invalid",
) -> bool:
    """Persist one OAuth event before acknowledgement; replayed deliveries are no-ops."""
    account_id = _uuid(instagram_account_id)
    external_id = _text(instagram_user_id, limit=64)
    normalized = _work_payload(event_type, value)
    base_url = _public_base_url(public_base_url)
    if account_id is None or not external_id or normalized is None or base_url is None:
        return False
    normalized_type, provider_event_id, payload = normalized
    work = InstagramOAuthWebhookWorkItem(
        instagram_account_id=account_id,
        instagram_user_id=external_id,
        provider_event_id=provider_event_id,
        event_type=normalized_type,
        payload=payload,
        public_base_url=base_url,
    )
    try:
        with session.begin_nested():
            session.add(work)
            session.flush()
    except IntegrityError:
        return False
    return True


def reap_expired_oauth_webhook_work_leases(
    session: Session, *, now: datetime | None = None
) -> int:
    """Make work abandoned by a process crash eligible for another safe claim."""
    current = _now(now)
    work_items = list(session.scalars(select(InstagramOAuthWebhookWorkItem).where(
        InstagramOAuthWebhookWorkItem.state == "processing",
        InstagramOAuthWebhookWorkItem.lease_expires_at.is_not(None),
        InstagramOAuthWebhookWorkItem.lease_expires_at <= current,
    ).with_for_update(skip_locked=True)).all())
    for item in work_items:
        item.state = "pending"
        item.lease_token = None
        item.lease_expires_at = None
    return len(work_items)


def claim_due_oauth_webhook_work(
    session: Session, *, now: datetime | None = None, limit: int = 25
) -> list[ClaimedOAuthWebhookWork]:
    """Lease pending ingress work; completed and terminal failures never replay."""
    current = _now(now)
    reap_expired_oauth_webhook_work_leases(session, now=current)
    work_items = list(session.scalars(select(InstagramOAuthWebhookWorkItem).where(
        InstagramOAuthWebhookWorkItem.state == "pending",
    ).order_by(
        InstagramOAuthWebhookWorkItem.created_at, InstagramOAuthWebhookWorkItem.id
    ).limit(max(1, min(int(limit), 100))).with_for_update(skip_locked=True)).all())
    claimed: list[ClaimedOAuthWebhookWork] = []
    for item in work_items:
        lease_token = secrets.token_urlsafe(32)
        item.state = "processing"
        item.lease_token = lease_token
        item.lease_expires_at = current + WEBHOOK_WORK_LEASE_DURATION
        item.attempt_count += 1
        claimed.append(ClaimedOAuthWebhookWork(
            id=item.id,
            instagram_account_id=item.instagram_account_id,
            instagram_user_id=item.instagram_user_id,
            provider_event_id=item.provider_event_id,
            event_type=item.event_type,
            payload=dict(item.payload),
            public_base_url=item.public_base_url,
            lease_token=lease_token,
        ))
    session.flush()
    return claimed


def _claimed_work(
    session: Session, work_id: object, lease_token: object
) -> InstagramOAuthWebhookWorkItem | None:
    identifier = _uuid(work_id)
    lease = _text(lease_token, limit=128)
    if identifier is None or not lease:
        return None
    return session.scalar(select(InstagramOAuthWebhookWorkItem).where(
        InstagramOAuthWebhookWorkItem.id == identifier,
        InstagramOAuthWebhookWorkItem.state == "processing",
        InstagramOAuthWebhookWorkItem.lease_token == lease,
    ).with_for_update())


def complete_oauth_webhook_work(
    session: Session, work_id: object, lease_token: object, *, now: datetime | None = None
) -> bool:
    item = _claimed_work(session, work_id, lease_token)
    if item is None:
        return False
    item.state = "completed"
    item.completed_at = _now(now)
    item.lease_token = None
    item.lease_expires_at = None
    return True


def fail_oauth_webhook_work(
    session: Session, work_id: object, lease_token: object, *, now: datetime | None = None
) -> bool:
    """Terminal only: unexpected processing exceptions must not replay outbound work."""
    item = _claimed_work(session, work_id, lease_token)
    if item is None:
        return False
    item.state = "failed"
    item.failed_at = _now(now)
    item.lease_token = None
    item.lease_expires_at = None
    return True


def oauth_webhook_work_account_matches(
    session: Session, *, instagram_account_id: object, instagram_user_id: object
) -> bool:
    """Reconfirm a claimed item remains bound to its original external account."""
    account_id = _uuid(instagram_account_id)
    external_id = _text(instagram_user_id, limit=64)
    if account_id is None or not external_id:
        return False
    account = session.get(InstagramAccount, account_id)
    return bool(
        account is not None
        and hmac.compare_digest(_text(account.instagram_user_id, limit=64), external_id)
    )
