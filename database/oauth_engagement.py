"""Durable OAuth-only engagement state; legacy JSON is deliberately excluded."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import InstagramEngagementEvent, InstagramOAuthDirectMessageInteraction, InstagramPendingAccess, InstagramPromptPost, InstagramPromptRequest, InstagramSmartReplyActivity


def claim_oauth_event(session: Session, account_id: object, provider_event_id: object, event_type: str) -> bool:
    """Claim a provider event once; duplicate/replayed claims never run again."""
    try:
        normalized_account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return False
    event_id = str(provider_event_id or "").strip()
    if not event_id or event_type not in {"comment", "message"}:
        return False
    try:
        with session.begin_nested():
            session.add(InstagramEngagementEvent(instagram_account_id=normalized_account, provider_event_id=event_id, event_type=event_type, state="processing"))
            session.flush()
        return True
    except IntegrityError:
        return False


def complete_oauth_event(session: Session, account_id: object, provider_event_id: object, event_type: str, *, failed: bool = False) -> None:
    """Finalize a claimed event after its required external action succeeds.

    A failed event is deliberately terminal in this checkpoint.  An unknown
    outcome from a remote Graph request must never be replayed automatically,
    because that could send a duplicate reply.  A later operator/reconciliation
    workflow can inspect failed events without weakening this guarantee.
    """
    try:
        account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return
    event = session.scalar(select(InstagramEngagementEvent).where(
        InstagramEngagementEvent.instagram_account_id == account,
        InstagramEngagementEvent.provider_event_id == str(provider_event_id or ""),
        InstagramEngagementEvent.event_type == event_type,
    ))
    if event is not None:
        event.state = "failed" if failed else "done"
        if failed:
            event.failed_at = datetime.now(timezone.utc)
        else:
            event.completed_at = datetime.now(timezone.utc)


def oauth_prompt_post(session: Session, account_id: object, media_id: object, prompts: list[dict[str, object]], *, permalink: str = "", cover_url: str = "") -> InstagramPromptPost:
    account = uuid.UUID(str(account_id))
    media = str(media_id or "").strip()
    record = session.scalar(select(InstagramPromptPost).where(InstagramPromptPost.instagram_account_id == account, InstagramPromptPost.media_id == media))
    if record is None:
        record = InstagramPromptPost(instagram_account_id=account, media_id=media, public_id=secrets.token_urlsafe(32), prompts=prompts, permalink=permalink or None, cover_url=cover_url or None)
        session.add(record)
        session.flush()
    return record


def get_oauth_prompt_post(session: Session, account_id: object, media_id: object) -> InstagramPromptPost | None:
    """Return one account-scoped prompt cache entry; never consult legacy JSON."""
    try:
        account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return None
    media = str(media_id or "").strip()
    if not media:
        return None
    return session.scalar(select(InstagramPromptPost).where(
        InstagramPromptPost.instagram_account_id == account,
        InstagramPromptPost.media_id == media,
    ))


def get_oauth_prompt_post_by_public_id(session: Session, public_id: object) -> InstagramPromptPost | None:
    """Resolve only an opaque public identifier for the OAuth prompt page."""
    identifier = str(public_id or "").strip()
    if not identifier or len(identifier) > 128:
        return None
    return session.scalar(select(InstagramPromptPost).where(
        InstagramPromptPost.public_id == identifier
    ))


def save_oauth_pending_access(session: Session, account_id: object, recipient_id: object, page_url: str) -> None:
    account = uuid.UUID(str(account_id)); recipient = str(recipient_id or "").strip()
    record = session.scalar(select(InstagramPendingAccess).where(InstagramPendingAccess.instagram_account_id == account, InstagramPendingAccess.recipient_id == recipient))
    if record is None:
        session.add(InstagramPendingAccess(instagram_account_id=account, recipient_id=recipient, page_url=page_url))
    else:
        record.page_url = page_url


def pop_oauth_pending_access(session: Session, account_id: object, recipient_id: object) -> str | None:
    record = session.scalar(select(InstagramPendingAccess).where(InstagramPendingAccess.instagram_account_id == uuid.UUID(str(account_id)), InstagramPendingAccess.recipient_id == str(recipient_id or "").strip()))
    if record is None:
        return None
    page_url = record.page_url
    session.delete(record)
    return page_url


def get_oauth_pending_access(session: Session, account_id: object, recipient_id: object) -> str | None:
    """Read pending access without consuming it before Graph delivery succeeds."""
    try:
        account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return None
    recipient = str(recipient_id or "").strip()
    if not recipient:
        return None
    record = session.scalar(select(InstagramPendingAccess).where(
        InstagramPendingAccess.instagram_account_id == account,
        InstagramPendingAccess.recipient_id == recipient,
    ))
    return record.page_url if record is not None else None


def record_oauth_request(session: Session, account_id: object, media_id: object, comment_id: object, recipient_id: object, username: object, page_url: str) -> None:
    session.add(InstagramPromptRequest(instagram_account_id=uuid.UUID(str(account_id)), media_id=str(media_id), comment_id=str(comment_id), recipient_id=str(recipient_id), username=str(username or "") or None, page_url=page_url))


def oauth_request_on_cooldown(session: Session, account_id: object, media_id: object, recipient_id: object, *, cutoff: datetime) -> bool:
    return session.scalar(select(InstagramPromptRequest.id).where(InstagramPromptRequest.instagram_account_id == uuid.UUID(str(account_id)), InstagramPromptRequest.media_id == str(media_id), InstagramPromptRequest.recipient_id == str(recipient_id), InstagramPromptRequest.created_at >= cutoff).limit(1)) is not None


def oauth_smart_reply_allowed(session: Session, account_id: object, *, cutoff: datetime, limit: int) -> bool:
    return len(session.scalars(select(InstagramSmartReplyActivity).where(InstagramSmartReplyActivity.instagram_account_id == uuid.UUID(str(account_id)), InstagramSmartReplyActivity.created_at >= cutoff)).all()) < limit


def record_oauth_smart_reply(session: Session, account_id: object, comment_id: object, recipient_id: object, username: object, reply_text: str) -> None:
    session.add(InstagramSmartReplyActivity(instagram_account_id=uuid.UUID(str(account_id)), comment_id=str(comment_id), recipient_id=str(recipient_id or "") or None, username=str(username or "") or None, reply_text=reply_text))


def record_oauth_direct_message_interaction(
    session: Session, account_id: object, source_comment_id: object, recipient_id: object,
    *, initial_private_reply_at: datetime | None = None,
) -> InstagramOAuthDirectMessageInteraction | None:
    """Persist a direct comment reply only after its initial private DM succeeded."""
    try:
        account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return None
    source = str(source_comment_id or "").strip()
    recipient = str(recipient_id or "").strip()
    if not source or not recipient:
        return None
    record = session.scalar(select(InstagramOAuthDirectMessageInteraction).where(
        InstagramOAuthDirectMessageInteraction.instagram_account_id == account,
        InstagramOAuthDirectMessageInteraction.source_comment_id == source,
    ))
    if record is not None:
        return record
    record = InstagramOAuthDirectMessageInteraction(
        instagram_account_id=account,
        source_comment_id=source,
        recipient_id=recipient,
        initial_private_reply_at=(initial_private_reply_at or datetime.now(timezone.utc)),
    )
    session.add(record)
    session.flush()
    return record


def consume_oauth_direct_message_interaction(
    session: Session, account_id: object, recipient_id: object, reply_message_id: object,
    *, replied_at: datetime | None = None,
) -> InstagramOAuthDirectMessageInteraction | None:
    """Atomically bind one account-scoped waiting interaction to one inbound reply."""
    try:
        account = uuid.UUID(str(account_id))
    except (TypeError, ValueError, AttributeError):
        return None
    recipient = str(recipient_id or "").strip()
    message_id = str(reply_message_id or "").strip()
    if not recipient or not message_id:
        return None
    reply_time = replied_at or datetime.now(timezone.utc)
    record = session.scalar(select(InstagramOAuthDirectMessageInteraction).where(
        InstagramOAuthDirectMessageInteraction.instagram_account_id == account,
        InstagramOAuthDirectMessageInteraction.recipient_id == recipient,
        InstagramOAuthDirectMessageInteraction.recipient_reply_at.is_(None),
        InstagramOAuthDirectMessageInteraction.initial_private_reply_at <= reply_time,
    ).order_by(
        InstagramOAuthDirectMessageInteraction.initial_private_reply_at.desc(),
        InstagramOAuthDirectMessageInteraction.id.desc(),
    ).limit(1).with_for_update())
    if record is None:
        return None
    record.recipient_reply_message_id = message_id
    record.recipient_reply_at = reply_time
    return record
