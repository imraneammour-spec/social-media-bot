"""Read-only, account-scoped view models for the OAuth tenant dashboard."""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    InstagramAccount,
    InstagramCredential,
    InstagramEngagementEvent,
    InstagramOAuthFollowupJob,
    InstagramPendingAccess,
    InstagramPromptPost,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
)
from .ownership import list_owned_instagram_accounts


REQUIRED_ENGAGEMENT_SCOPES = {
    "instagram_business_manage_comments",
    "instagram_business_manage_messages",
}
RECONNECT_STATUSES = {"disconnected", "error", "reconnect_required"}


@dataclass(frozen=True)
class TenantAccount:
    """Trusted-only account selection data; the identifier is never serialized."""

    instagram_account_id: uuid.UUID
    username: str
    connection_label: str
    connection_detail: str
    activation_label: str
    can_reconnect: bool
    can_disconnect: bool


@dataclass(frozen=True)
class TenantLead:
    username: str
    requested_at: str
    prompt_state: str
    public_prompt_id: str | None


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _username(value: object) -> str:
    name = str(value or "").strip()
    return name if name else "Instagram user"


def _connection(account: InstagramAccount) -> tuple[str, str, bool, bool]:
    status = str(account.connection_status or "").strip().lower()
    has_error = bool(str(account.last_connection_error_code or "").strip())
    if status == "connected" and not has_error:
        return "Connected", "This account is connected.", False, True
    if status == "connected" or status in RECONNECT_STATUSES:
        return "Reconnect required", "This account needs reconnection before it can be used.", True, status != "disconnected"
    if status == "disconnected":
        return "Disconnected", "This account is disconnected from this app.", True, False
    return "Connection setup incomplete", "Connection setup is incomplete.", False, False


def _activation_label(credential: InstagramCredential | None, connection_label: str) -> str:
    scopes = credential.scopes if credential is not None else None
    granted = {item for item in scopes if isinstance(item, str)} if isinstance(scopes, list) else set()
    if connection_label == "Connected" and REQUIRED_ENGAGEMENT_SCOPES.issubset(granted):
        return "Activation ready"
    return "Activation not ready"


def list_tenant_accounts(
    session: Session,
    *,
    user_id: object,
    legacy_instagram_user_id: object,
) -> list[TenantAccount]:
    """List a user's non-legacy accounts in existing deterministic order."""

    legacy_id = str(legacy_instagram_user_id or "").strip()
    accounts = [
        account
        for account in list_owned_instagram_accounts(session, user_id=user_id)
        if not legacy_id or not hmac.compare_digest(str(account.instagram_user_id), legacy_id)
    ]
    if not accounts:
        return []
    credentials = {
        credential.instagram_account_id: credential
        for credential in session.scalars(
            select(InstagramCredential).where(
                InstagramCredential.instagram_account_id.in_([account.id for account in accounts])
            )
        )
    }
    result: list[TenantAccount] = []
    for account in accounts:
        label, detail, can_reconnect, can_disconnect = _connection(account)
        result.append(
            TenantAccount(
                instagram_account_id=account.id,
                username=_username(account.username),
                connection_label=label,
                connection_detail=detail,
                activation_label=_activation_label(credentials.get(account.id), label),
                can_reconnect=can_reconnect,
                can_disconnect=can_disconnect,
            )
        )
    return result


def overview(session: Session, *, instagram_account_id: uuid.UUID) -> dict[str, int]:
    """Return only aggregate, account-scoped tenant counts."""

    def count(model) -> int:
        return len(
            session.scalars(
                select(model.id).where(model.instagram_account_id == instagram_account_id)
            ).all()
        )

    return {
        "lead_count": count(InstagramPromptRequest),
        "activity_count": count(InstagramSmartReplyActivity) + count(InstagramEngagementEvent),
        "pending_access_count": count(InstagramPendingAccess),
        "prompt_post_count": count(InstagramPromptPost),
        "followup_count": count(InstagramOAuthFollowupJob),
    }


def leads(session: Session, *, instagram_account_id: uuid.UUID) -> list[TenantLead]:
    """Return sanitized lead records for exactly one owned account."""

    requests = list(
        session.scalars(
            select(InstagramPromptRequest)
            .where(InstagramPromptRequest.instagram_account_id == instagram_account_id)
            .order_by(InstagramPromptRequest.created_at.desc(), InstagramPromptRequest.id.desc())
            .limit(100)
        )
    )
    media_ids = [request.media_id for request in requests]
    prompt_posts = {
        post.media_id: post.public_id
        for post in session.scalars(
            select(InstagramPromptPost).where(
                InstagramPromptPost.instagram_account_id == instagram_account_id,
                InstagramPromptPost.media_id.in_(media_ids),
            )
        )
    } if media_ids else {}
    return [
        TenantLead(
            username=_username(request.username),
            requested_at=_timestamp(request.created_at) or "",
            prompt_state="Prompts prepared" if request.media_id in prompt_posts else "Request received",
            public_prompt_id=prompt_posts.get(request.media_id),
        )
        for request in requests
    ]


def activity(session: Session, *, instagram_account_id: uuid.UUID) -> list[dict[str, str]]:
    """Build a generic activity feed without provider IDs or message content."""

    records: list[dict[str, str]] = []
    for request in session.scalars(
        select(InstagramPromptRequest)
        .where(InstagramPromptRequest.instagram_account_id == instagram_account_id)
        .order_by(InstagramPromptRequest.created_at.desc())
        .limit(100)
    ):
        records.append({
            "occurred_at": _timestamp(request.created_at) or "",
            "type": "Prompt request",
            "state": "Completed",
            "username": _username(request.username),
        })
    for reply in session.scalars(
        select(InstagramSmartReplyActivity)
        .where(InstagramSmartReplyActivity.instagram_account_id == instagram_account_id)
        .order_by(InstagramSmartReplyActivity.created_at.desc())
        .limit(100)
    ):
        records.append({
            "occurred_at": _timestamp(reply.created_at) or "",
            "type": "Reply sent",
            "state": "Completed",
            "username": _username(reply.username),
        })
    state_labels = {"done": "Completed", "processing": "Waiting", "failed": "Unavailable"}
    type_labels = {"comment": "Comment received", "message": "Message received"}
    for event in session.scalars(
        select(InstagramEngagementEvent)
        .where(InstagramEngagementEvent.instagram_account_id == instagram_account_id)
        .order_by(InstagramEngagementEvent.created_at.desc())
        .limit(100)
    ):
        records.append({
            "occurred_at": _timestamp(event.created_at) or "",
            "type": type_labels.get(event.event_type, "Engagement activity"),
            "state": state_labels.get(event.state, "Unavailable"),
            "username": "",
        })
    return sorted(records, key=lambda record: record["occurred_at"], reverse=True)[:100]


def followups(session: Session, *, instagram_account_id: uuid.UUID) -> list[dict[str, str | None]]:
    """Return a safe status feed for one account's durable follow-up jobs."""

    labels = {
        "pending": "Scheduled",
        "processing": "Sending",
        "sent": "Sent",
        "cancelled": "Cancelled after reply",
        "failed": "Delivery unavailable",
    }
    jobs = session.scalars(
        select(InstagramOAuthFollowupJob)
        .where(InstagramOAuthFollowupJob.instagram_account_id == instagram_account_id)
        .order_by(InstagramOAuthFollowupJob.created_at.desc(), InstagramOAuthFollowupJob.id.desc())
        .limit(100)
    )
    return [
        {
            "status": labels.get(job.state, "Delivery unavailable"),
            "scheduled_at": _timestamp(job.run_at),
            "completed_at": _timestamp(job.sent_at or job.cancelled_at or job.failed_at),
        }
        for job in jobs
    ]
