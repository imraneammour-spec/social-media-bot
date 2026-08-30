"""Read-only, account-scoped view models for the OAuth tenant dashboard."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select
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
from .oauth_capability import assess_oauth_engagement_records


RECONNECT_STATUSES = {"disconnected", "error", "reconnect_required"}
LEAD_STATUSES = ("new", "contacted", "qualified", "won", "lost")
LEAD_VIEWS = ("active", "archived", "all")
LEAD_SORTS = ("newest", "oldest")
LEADS_PER_PAGE = 25
MAX_LEAD_PAGE = 1000
MAX_LEAD_SEARCH_LENGTH = 80
LEAD_ACTION_REFERENCE_VERSION = "v1"
LEAD_ACTION_REFERENCE_TTL_SECONDS = 900
LEAD_ACTION_PURPOSES = {"lead-status", "lead-note", "lead-archive"}
ACTIVITY_EVENT_TYPES = (
    "all",
    "prompt_requests",
    "smart_replies",
    "webhook_events",
    "followups",
)
ACTIVITY_EVENT_TYPE_LABELS = (
    ("all", "All activity"),
    ("prompt_requests", "Prompt requests"),
    ("smart_replies", "Smart replies"),
    ("webhook_events", "Webhook events"),
    ("followups", "Follow-ups"),
)


@dataclass(frozen=True)
class TenantAccount:
    """Trusted-only account selection data; the identifier is never serialized."""

    instagram_account_id: uuid.UUID
    username: str
    connection_label: str
    connection_detail: str
    activation_label: str
    activation_detail: str
    can_reconnect: bool
    can_disconnect: bool


@dataclass(frozen=True)
class TenantLead:
    """Trusted-only lead data; identifiers are converted to opaque references by routes."""

    prompt_request_id: uuid.UUID
    username: str
    requested_at: str
    prompt_state: str
    base_stage: str
    base_stage_label: str
    archived: bool
    archived_label: str
    internal_note: str | None
    public_prompt_id: str | None
    post_permalink: str | None
    cover_url: str | None


@dataclass(frozen=True)
class TenantLeadPage:
    """A bounded, account-scoped page of sanitized leads."""

    leads: list[TenantLead]
    page: int
    has_previous: bool
    has_next: bool


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _username(value: object) -> str:
    name = str(value or "").strip()
    return name if name else "Instagram user"


def _public_https_url(value: object) -> str | None:
    """Return a render-safe public HTTPS URL, or no URL."""

    url = str(value or "").strip()
    if not url or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or host == "localhost"
        or host.endswith(".local")
        or host.endswith(".internal")
        or host.endswith(".invalid")
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return url
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified:
        return None
    return url


def _instagram_permalink(value: object) -> str | None:
    """Return a public Instagram permalink only."""

    url = _public_https_url(value)
    if url is None:
        return None
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    return url if host == "instagram.com" or host.endswith(".instagram.com") else None


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _lead_status_label(value: object) -> str:
    labels = {
        "new": "New",
        "contacted": "Contacted",
        "qualified": "Qualified",
        "won": "Won",
        "lost": "Lost",
    }
    return labels.get(str(value or "").strip().lower(), "New")


def _lead_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in LEAD_STATUSES else "new"


def lead_filters(
    view: object = None,
    stage: object = None,
    search: object = None,
    sort: object = None,
    page: object = None,
) -> tuple[str, str | None, str, str, int]:
    """Normalize lead filters without accepting values as query syntax."""

    normalized_view = str(view or "active").strip().lower()
    if normalized_view not in LEAD_VIEWS:
        normalized_view = "active"
    normalized_stage = str(stage or "").strip().lower()
    normalized_search = str(search or "").strip()[:MAX_LEAD_SEARCH_LENGTH]
    normalized_sort = str(sort or "newest").strip().lower()
    if normalized_sort not in LEAD_SORTS:
        normalized_sort = "newest"
    try:
        normalized_page = int(str(page or "1").strip())
    except (TypeError, ValueError):
        normalized_page = 1
    normalized_page = min(max(normalized_page, 1), MAX_LEAD_PAGE)
    return (
        normalized_view,
        normalized_stage if normalized_stage in LEAD_STATUSES else None,
        normalized_search,
        normalized_sort,
        normalized_page,
    )


def activity_filter(value: object = None) -> str:
    """Normalize the finite Activity event-type filter."""

    normalized = str(value or "all").strip().lower()
    return normalized if normalized in ACTIVITY_EVENT_TYPES else "all"


def _lead_action_payload(
    user_id: object,
    instagram_account_id: object,
    prompt_request_id: object,
    purpose: str,
    expires_at: int,
) -> str:
    return (
        f"{LEAD_ACTION_REFERENCE_VERSION}|tenant-lead|{purpose}|{user_id}|"
        f"{instagram_account_id}|{prompt_request_id}|{expires_at}"
    )


def lead_action_reference_for_prompt_request(
    user_id: object,
    instagram_account_id: object,
    prompt_request_id: object,
    purpose: str,
    *,
    secret_key: object,
    now: int | None = None,
) -> str:
    """Create a non-revealing, purpose-bound action reference for one lead."""

    if purpose not in LEAD_ACTION_PURPOSES:
        return ""
    current = int(time.time() if now is None else now)
    expires_at = current + LEAD_ACTION_REFERENCE_TTL_SECONDS
    payload = _lead_action_payload(
        user_id, instagram_account_id, prompt_request_id, purpose, expires_at
    )
    signature = hmac.new(
        str(secret_key).encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).digest()
    encoded = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return f"{LEAD_ACTION_REFERENCE_VERSION}.{expires_at}.{encoded}"


def resolve_tenant_lead_action_reference(
    session: Session,
    *,
    user_id: object,
    instagram_account_id: object,
    reference: object,
    purpose: str,
    secret_key: object,
    now: int | None = None,
) -> InstagramPromptRequest | None:
    """Resolve an action only within the already-selected owned account.

    The wire reference deliberately contains no identifier. Candidate leads are
    scoped by the selected account before their expected HMAC is compared.
    """

    account_id = _uuid(instagram_account_id)
    if account_id is None or purpose not in LEAD_ACTION_PURPOSES or not isinstance(reference, str):
        return None
    parts = reference.split(".")
    if len(parts) != 3 or parts[0] != LEAD_ACTION_REFERENCE_VERSION or not parts[1].isdigit():
        return None
    current = int(time.time() if now is None else now)
    expires_at = int(parts[1])
    if expires_at < current or expires_at > current + LEAD_ACTION_REFERENCE_TTL_SECONDS:
        return None
    candidate_ids = session.scalars(
        select(InstagramPromptRequest.id).where(
            InstagramPromptRequest.instagram_account_id == account_id
        )
    )
    for request_id in candidate_ids:
        expected = lead_action_reference_for_prompt_request(
            user_id,
            account_id,
            request_id,
            purpose,
            secret_key=secret_key,
            now=expires_at - LEAD_ACTION_REFERENCE_TTL_SECONDS,
        )
        if hmac.compare_digest(expected, reference):
            return session.scalar(
                select(InstagramPromptRequest)
                .where(
                    InstagramPromptRequest.id == request_id,
                    InstagramPromptRequest.instagram_account_id == account_id,
                )
                .with_for_update()
            )
    return None


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


def _activation_status(
    account: InstagramAccount,
    credential: InstagramCredential | None,
    connection_label: str,
) -> tuple[str, str]:
    if connection_label == "Connection setup incomplete":
        return "Connection setup incomplete", "Connection setup is incomplete."
    assessment = assess_oauth_engagement_records(account, credential)
    labels = {
        "active": (
            "Active",
            "Instagram engagement automation is active.",
        ),
        "ready_for_activation": (
            "Ready for activation",
            "Instagram is connected and ready, but engagement automation is not enabled yet.",
        ),
        "reconnect_required": (
            "Reconnect required",
            "Reconnect Instagram to restore automation access.",
        ),
        "temporarily_unavailable": (
            "Temporarily unavailable",
            "Automation status is temporarily unavailable.",
        ),
    }
    return labels.get(assessment.state, labels["temporarily_unavailable"])


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
        activation_label, activation_detail = _activation_status(
            account,
            credentials.get(account.id),
            label,
        )
        result.append(
            TenantAccount(
                instagram_account_id=account.id,
                username=_username(account.username),
                connection_label=label,
                connection_detail=detail,
                activation_label=activation_label,
                activation_detail=activation_detail,
                can_reconnect=can_reconnect,
                can_disconnect=can_disconnect,
            )
        )
    return result


def overview(session: Session, *, instagram_account_id: uuid.UUID) -> dict[str, int]:
    """Return only aggregate, account-scoped tenant counts."""

    def count(model) -> int:
        return int(
            session.scalar(
                select(func.count())
                .select_from(model)
                .where(model.instagram_account_id == instagram_account_id)
            )
            or 0
        )

    lead_stage_counts = {status: 0 for status in LEAD_STATUSES}
    for status, total in session.execute(
        select(InstagramPromptRequest.lead_status, func.count())
        .where(
            InstagramPromptRequest.instagram_account_id == instagram_account_id,
            InstagramPromptRequest.lead_archived_at.is_(None),
        )
        .group_by(InstagramPromptRequest.lead_status)
    ):
        normalized_status = _lead_status(status)
        if str(status or "").strip().lower() in LEAD_STATUSES:
            lead_stage_counts[normalized_status] = int(total)

    followup_state_counts = {
        "scheduled": 0,
        "sending": 0,
        "sent": 0,
        "cancelled": 0,
        "unavailable": 0,
    }
    followup_labels = {
        "pending": "scheduled",
        "processing": "sending",
        "sent": "sent",
        "cancelled": "cancelled",
    }
    for state, total in session.execute(
        select(InstagramOAuthFollowupJob.state, func.count())
        .where(InstagramOAuthFollowupJob.instagram_account_id == instagram_account_id)
        .group_by(InstagramOAuthFollowupJob.state)
    ):
        label = followup_labels.get(str(state or "").strip().lower(), "unavailable")
        followup_state_counts[label] += int(total)

    return {
        "lead_count": count(InstagramPromptRequest),
        "lead_stage_counts": lead_stage_counts,
        "archived_lead_count": int(
            session.scalar(
                select(func.count())
                .select_from(InstagramPromptRequest)
                .where(
                    InstagramPromptRequest.instagram_account_id == instagram_account_id,
                    InstagramPromptRequest.lead_archived_at.is_not(None),
                )
            )
            or 0
        ),
        "activity_count": count(InstagramSmartReplyActivity) + count(InstagramEngagementEvent),
        "pending_access_count": count(InstagramPendingAccess),
        "prompt_post_count": count(InstagramPromptPost),
        "followup_count": count(InstagramOAuthFollowupJob),
        "followup_state_counts": followup_state_counts,
        "recent_activity": activity(session, instagram_account_id=instagram_account_id, limit=5),
    }


def leads(
    session: Session,
    *,
    instagram_account_id: uuid.UUID,
    view: object = None,
    stage: object = None,
    search: object = None,
    sort: object = None,
    page: object = None,
) -> TenantLeadPage:
    """Return sanitized lead records for exactly one owned account."""

    selected_view, selected_stage, selected_search, selected_sort, selected_page = lead_filters(
        view, stage, search, sort, page
    )
    query = select(InstagramPromptRequest).where(
        InstagramPromptRequest.instagram_account_id == instagram_account_id
    )
    if selected_view == "active":
        query = query.where(InstagramPromptRequest.lead_archived_at.is_(None))
    elif selected_view == "archived":
        query = query.where(InstagramPromptRequest.lead_archived_at.is_not(None))
    if selected_stage is not None:
        query = query.where(InstagramPromptRequest.lead_status == selected_stage)
    if selected_search:
        escaped_search = (
            selected_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        query = query.where(
            InstagramPromptRequest.username.ilike(f"%{escaped_search}%", escape="\\")
        )
    ordering = (
        (InstagramPromptRequest.created_at.asc(), InstagramPromptRequest.id.asc())
        if selected_sort == "oldest"
        else (InstagramPromptRequest.created_at.desc(), InstagramPromptRequest.id.desc())
    )
    requests = list(
        session.scalars(
            query
            .order_by(*ordering)
            .offset((selected_page - 1) * LEADS_PER_PAGE)
            .limit(LEADS_PER_PAGE + 1)
        )
    )
    has_next = len(requests) > LEADS_PER_PAGE
    requests = requests[:LEADS_PER_PAGE]
    media_ids = [request.media_id for request in requests]
    prompt_posts = {
        post.media_id: post
        for post in session.scalars(
            select(InstagramPromptPost).where(
                InstagramPromptPost.instagram_account_id == instagram_account_id,
                InstagramPromptPost.media_id.in_(media_ids),
            )
        )
    } if media_ids else {}
    return TenantLeadPage(
        leads=[
            TenantLead(
                prompt_request_id=request.id,
                username=_username(request.username),
                requested_at=_timestamp(request.created_at) or "",
                prompt_state="Prompts prepared" if request.media_id in prompt_posts else "Request received",
                base_stage=_lead_status(request.lead_status),
                base_stage_label=_lead_status_label(request.lead_status),
                archived=request.lead_archived_at is not None,
                archived_label="Archived" if request.lead_archived_at is not None else "Active",
                internal_note=request.internal_note or None,
                public_prompt_id=(
                    prompt_posts[request.media_id].public_id
                    if request.media_id in prompt_posts
                    else None
                ),
                post_permalink=(
                    _instagram_permalink(prompt_posts[request.media_id].permalink)
                    if request.media_id in prompt_posts
                    else None
                ),
                cover_url=(
                    _public_https_url(prompt_posts[request.media_id].cover_url)
                    if request.media_id in prompt_posts
                    else None
                ),
            )
            for request in requests
        ],
        page=selected_page,
        has_previous=selected_page > 1,
        has_next=has_next,
    )


def activity(
    session: Session,
    *,
    instagram_account_id: uuid.UUID,
    event_type: object = None,
    limit: int = 100,
) -> list[dict[str, str]]:
    """Build a generic activity feed without provider IDs or message content."""

    selected_event_type = activity_filter(event_type)
    records: list[dict[str, str]] = []
    if selected_event_type in {"all", "prompt_requests"}:
        for request in session.scalars(
            select(InstagramPromptRequest)
            .where(InstagramPromptRequest.instagram_account_id == instagram_account_id)
            .order_by(InstagramPromptRequest.created_at.desc())
            .limit(limit)
        ):
            records.append({
                "occurred_at": _timestamp(request.created_at) or "",
                "type": "Prompt request",
                "state": "Completed",
                "username": _username(request.username),
            })
    if selected_event_type in {"all", "smart_replies"}:
        for reply in session.scalars(
            select(InstagramSmartReplyActivity)
            .where(InstagramSmartReplyActivity.instagram_account_id == instagram_account_id)
            .order_by(InstagramSmartReplyActivity.created_at.desc())
            .limit(limit)
        ):
            records.append({
                "occurred_at": _timestamp(reply.created_at) or "",
                "type": "Reply sent",
                "state": "Completed",
                "username": _username(reply.username),
            })
    state_labels = {"done": "Completed", "processing": "Waiting", "failed": "Unavailable"}
    type_labels = {"comment": "Comment received", "message": "Message received"}
    if selected_event_type in {"all", "webhook_events"}:
        for event in session.scalars(
            select(InstagramEngagementEvent)
            .where(InstagramEngagementEvent.instagram_account_id == instagram_account_id)
            .order_by(InstagramEngagementEvent.created_at.desc())
            .limit(limit)
        ):
            records.append({
                "occurred_at": _timestamp(event.created_at) or "",
                "type": type_labels.get(event.event_type, "Engagement activity"),
                "state": state_labels.get(event.state, "Unavailable"),
                "username": "",
            })
    if selected_event_type in {"all", "followups"}:
        followup_labels = {
            "pending": ("Follow-up scheduled", "Waiting", None),
            "processing": ("Follow-up scheduled", "Waiting", None),
            "sent": ("Follow-up sent", "Completed", "sent_at"),
            "cancelled": ("Follow-up cancelled", "Completed", "cancelled_at"),
            "failed": ("Follow-up failed", "Unavailable", "failed_at"),
        }
        for job in session.scalars(
            select(InstagramOAuthFollowupJob)
            .where(InstagramOAuthFollowupJob.instagram_account_id == instagram_account_id)
            .order_by(InstagramOAuthFollowupJob.created_at.desc())
            .limit(limit)
        ):
            label = followup_labels.get(str(job.state or "").strip().lower())
            if label is None:
                continue
            event_name, state, completed_at = label
            occurred_at = getattr(job, completed_at) if completed_at else None
            records.append({
                "occurred_at": _timestamp(occurred_at or job.created_at) or "",
                "type": event_name,
                "state": state,
                "username": "",
            })
    return sorted(records, key=lambda record: record["occurred_at"], reverse=True)[:limit]


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


# ---------------------------------------------------------------------------
# Phase 5C — Messages & Content view models
# ---------------------------------------------------------------------------

MESSAGE_FILTERS = ("all", "prompt", "messages", "followups", "smart_replies")
MESSAGE_FILTER_LABELS = (
    ("all", "All"),
    ("prompt", "Prompt interactions"),
    ("messages", "Messages"),
    ("followups", "Follow-ups"),
    ("smart_replies", "Smart replies"),
)
MESSAGES_PER_PAGE = 25
MAX_MESSAGE_PAGE = 1000

CONTENT_FILTERS = ("all", "prompt_enabled")
CONTENT_FILTER_LABELS = (
    ("all", "All"),
    ("prompt_enabled", "Prompt-enabled"),
)
CONTENT_PER_PAGE = 25
MAX_CONTENT_PAGE = 1000


def message_filter(value: object = None) -> str:
    """Normalize the finite Messages interaction-type filter."""

    normalized = str(value or "all").strip().lower()
    return normalized if normalized in MESSAGE_FILTERS else "all"


def _message_page(value: object) -> int:
    try:
        page = int(str(value or "1").strip())
    except (TypeError, ValueError):
        page = 1
    return min(max(page, 1), MAX_MESSAGE_PAGE)


@dataclass(frozen=True)
class TenantMessagePage:
    """A bounded, account-scoped page of sanitized interaction records."""

    messages: list[dict[str, str]]
    page: int
    has_previous: bool
    has_next: bool


def messages(
    session: Session,
    *,
    instagram_account_id: uuid.UUID,
    filter_value: object = None,
    page: object = None,
) -> TenantMessagePage:
    """Return sanitized interaction records for exactly one owned account.

    The backend does not persist inbound Instagram message text, so this is
    a *persisted-interaction* view — not a true inbox.  Each row is built from
    engagement events, prompt requests, smart replies, and follow-up jobs
    that are already scoped by ``instagram_account_id``.
    """

    selected_filter = message_filter(filter_value)
    selected_page = _message_page(page)
    limit = MESSAGES_PER_PAGE
    offset = (selected_page - 1) * limit

    records: list[dict[str, str]] = []

    if selected_filter in {"all", "prompt"}:
        for request in session.scalars(
            select(InstagramPromptRequest)
            .where(InstagramPromptRequest.instagram_account_id == instagram_account_id)
            .order_by(InstagramPromptRequest.created_at.desc())
            .limit(limit + 1)
            .offset(offset)
        ):
            records.append({
                "occurred_at": _timestamp(request.created_at) or "",
                "type": "Prompt request",
                "status": "Completed",
                "username": _username(request.username),
                "detail": "Trigger comment received",
            })

    if selected_filter in {"all", "smart_replies"}:
        for reply in session.scalars(
            select(InstagramSmartReplyActivity)
            .where(InstagramSmartReplyActivity.instagram_account_id == instagram_account_id)
            .order_by(InstagramSmartReplyActivity.created_at.desc())
            .limit(limit + 1)
            .offset(offset)
        ):
            records.append({
                "occurred_at": _timestamp(reply.created_at) or "",
                "type": "Smart reply",
                "status": "Completed",
                "username": _username(reply.username),
                "detail": "Automated reply sent",
            })

    if selected_filter in {"all", "messages"}:
        type_labels = {"comment": "Comment received", "message": "Incoming message"}
        state_labels = {"done": "Completed", "processing": "Processing", "failed": "Failed"}
        for event in session.scalars(
            select(InstagramEngagementEvent)
            .where(InstagramEngagementEvent.instagram_account_id == instagram_account_id)
            .order_by(InstagramEngagementEvent.created_at.desc())
            .limit(limit + 1)
            .offset(offset)
        ):
            records.append({
                "occurred_at": _timestamp(event.created_at) or "",
                "type": type_labels.get(event.event_type, "Engagement event"),
                "status": state_labels.get(event.state, "Unknown"),
                "username": "",
                "detail": "",
            })

    if selected_filter in {"all", "followups"}:
        followup_labels = {
            "pending": ("Follow-up scheduled", "Waiting"),
            "processing": ("Follow-up scheduled", "Sending"),
            "sent": ("Follow-up sent", "Completed"),
            "cancelled": ("Follow-up cancelled", "Cancelled"),
            "failed": ("Follow-up failed", "Failed"),
        }
        for job in session.scalars(
            select(InstagramOAuthFollowupJob)
            .where(InstagramOAuthFollowupJob.instagram_account_id == instagram_account_id)
            .order_by(InstagramOAuthFollowupJob.created_at.desc())
            .limit(limit + 1)
            .offset(offset)
        ):
            label = followup_labels.get(str(job.state or "").strip().lower())
            if label is None:
                continue
            event_name, state = label
            records.append({
                "occurred_at": _timestamp(job.created_at) or "",
                "type": event_name,
                "status": state,
                "username": "",
                "detail": "",
            })

    records.sort(key=lambda record: record["occurred_at"], reverse=True)
    has_next = len(records) > limit
    records = records[:limit]
    return TenantMessagePage(
        messages=records,
        page=selected_page,
        has_previous=selected_page > 1,
        has_next=has_next,
    )


def content_filter(value: object = None) -> str:
    """Normalize the finite Content filter."""

    normalized = str(value or "all").strip().lower()
    return normalized if normalized in CONTENT_FILTERS else "all"


def _content_page(value: object) -> int:
    try:
        page = int(str(value or "1").strip())
    except (TypeError, ValueError):
        page = 1
    return min(max(page, 1), MAX_CONTENT_PAGE)


@dataclass(frozen=True)
class TenantContentPage:
    """A bounded, account-scoped page of sanitized content records."""

    content: list[dict[str, str | None]]
    page: int
    has_previous: bool
    has_next: bool


def content(
    session: Session,
    *,
    instagram_account_id: uuid.UUID,
    filter_value: object = None,
    page: object = None,
) -> TenantContentPage:
    """Return sanitized prompt-post content records for one owned account.

    Only ``InstagramPromptPost`` records are shown — these are Instagram posts
    for which the bot has extracted and prepared prompts.  No draft or
    scheduled-post models exist in the current schema, so those filters are
    not offered.  External URLs are sanitized via the existing URL safety
    helpers already used in Leads.
    """

    selected_filter = content_filter(filter_value)
    selected_page = _content_page(page)
    limit = CONTENT_PER_PAGE
    offset = (selected_page - 1) * limit

    query = select(InstagramPromptPost).where(
        InstagramPromptPost.instagram_account_id == instagram_account_id
    )
    if selected_filter == "prompt_enabled":
        query = query.where(InstagramPromptPost.prompts.isnot(None))

    posts = list(
        session.scalars(
            query
            .order_by(InstagramPromptPost.created_at.desc(), InstagramPromptPost.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
    )
    has_next = len(posts) > limit
    posts = posts[:limit]

    content_rows = []
    for post in posts:
        prompt_count = len(post.prompts) if isinstance(post.prompts, list) else 0
        content_rows.append({
            "prompt_url": f"/p/o/{post.public_id}",
            "public_id": post.public_id,
            "permalink": _instagram_permalink(post.permalink),
            "cover_url": _public_https_url(post.cover_url),
            "prompt_count": str(prompt_count),
            "prompt_state": "Prompts prepared" if prompt_count > 0 else "No prompts",
            "created_at": _timestamp(post.created_at) or "",
        })

    return TenantContentPage(
        content=content_rows,
        page=selected_page,
        has_previous=selected_page > 1,
        has_next=has_next,
    )
