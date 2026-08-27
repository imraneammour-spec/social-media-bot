"""Account-scoped OAuth delayed follow-up persistence and job coordination."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import InstagramAccount, InstagramOAuthFollowupJob, InstagramPromptRequest


FOLLOWUP_DELAY = timedelta(hours=6)
FOLLOWUP_COOLDOWN = timedelta(days=7)
LEASE_DURATION = timedelta(minutes=15)
MAX_ATTEMPTS = 3
RETRYABLE_ERROR_CODES = {"credential_unavailable", "database_unavailable"}
TERMINAL_ERROR_CODES = {
    "ambiguous_graph_failure",
    "capability_unavailable",
    "lease_expired",
    "provider_rejected",
}


@dataclass(frozen=True)
class ClaimedOAuthFollowup:
    id: uuid.UUID
    instagram_account_id: uuid.UUID
    source_message_id: str
    recipient_id: str
    page_url: str
    lease_token: str
    state: str = "processing"


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return current.replace(tzinfo=timezone.utc) if current.tzinfo is None else current.astimezone(timezone.utc)


def _uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _text(value: object) -> str:
    return str(value or "").strip()


def schedule_oauth_followup(
    session: Session,
    *,
    instagram_account_id: object,
    source_message_id: object,
    recipient_id: object,
    page_url: object,
    prompt_request_id: object | None = None,
    run_at: datetime | None = None,
    now: datetime | None = None,
) -> InstagramOAuthFollowupJob | None:
    """Create one account-scoped delayed follow-up, or return its replay/active job."""
    account_id = _uuid(instagram_account_id)
    source_id, recipient, url = _text(source_message_id), _text(recipient_id), _text(page_url)
    request_id = _uuid(prompt_request_id) if prompt_request_id is not None else None
    current = _now(now)
    if account_id is None or not source_id or not recipient or not url.startswith("https://"):
        return None
    if request_id is not None:
        request_record = session.scalar(select(InstagramPromptRequest).where(
            InstagramPromptRequest.id == request_id,
            InstagramPromptRequest.instagram_account_id == account_id,
        ))
        if request_record is None:
            return None
    existing = session.scalar(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.instagram_account_id == account_id,
        InstagramOAuthFollowupJob.source_message_id == source_id,
    ))
    if existing is not None:
        return existing
    active = session.scalar(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.instagram_account_id == account_id,
        InstagramOAuthFollowupJob.recipient_id == recipient,
        InstagramOAuthFollowupJob.state.in_(("pending", "processing")),
    ))
    if active is not None:
        return active
    recent_sent = session.scalar(select(InstagramOAuthFollowupJob.id).where(
        InstagramOAuthFollowupJob.instagram_account_id == account_id,
        InstagramOAuthFollowupJob.recipient_id == recipient,
        InstagramOAuthFollowupJob.state == "sent",
        InstagramOAuthFollowupJob.sent_at >= current - FOLLOWUP_COOLDOWN,
    ).limit(1))
    if recent_sent is not None:
        return None
    job = InstagramOAuthFollowupJob(
        instagram_account_id=account_id,
        prompt_request_id=request_id,
        source_message_id=source_id,
        recipient_id=recipient,
        page_url=url,
        run_at=_now(run_at) if run_at is not None else current + FOLLOWUP_DELAY,
    )
    session.add(job)
    session.flush()
    return job


def cancel_oauth_followups(session: Session, *, instagram_account_id: object, recipient_id: object, now: datetime | None = None) -> int:
    """Cancel pending work for only one account/recipient pair."""
    account_id = _uuid(instagram_account_id)
    recipient = _text(recipient_id)
    if account_id is None or not recipient:
        return 0
    jobs = list(session.scalars(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.instagram_account_id == account_id,
        InstagramOAuthFollowupJob.recipient_id == recipient,
        InstagramOAuthFollowupJob.state == "pending",
    ).with_for_update()).all())
    current = _now(now)
    for job in jobs:
        job.state = "cancelled"
        job.cancelled_at = current
        job.lease_token = None
        job.lease_expires_at = None
    return len(jobs)


def claim_due_oauth_followups(session: Session, *, now: datetime | None = None, limit: int = 10) -> list[ClaimedOAuthFollowup]:
    """Atomically lease due pending work; PostgreSQL uses SKIP LOCKED."""
    current = _now(now)
    jobs = list(session.scalars(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.state == "pending",
        InstagramOAuthFollowupJob.run_at <= current,
    ).order_by(InstagramOAuthFollowupJob.run_at, InstagramOAuthFollowupJob.id).limit(max(1, min(int(limit), 100))).with_for_update(skip_locked=True)).all())
    claimed = []
    for job in jobs:
        lease_token = secrets.token_urlsafe(32)
        job.state = "processing"
        job.claimed_at = current
        job.lease_token = lease_token
        job.lease_expires_at = current + LEASE_DURATION
        claimed.append(ClaimedOAuthFollowup(
            id=job.id,
            instagram_account_id=job.instagram_account_id,
            source_message_id=job.source_message_id,
            recipient_id=job.recipient_id,
            page_url=job.page_url,
            lease_token=lease_token,
        ))
    session.flush()
    return claimed


def finalize_oauth_followup_success(session: Session, job_id: object, lease_token: object, *, now: datetime | None = None) -> bool:
    job = _claimed_job(session, job_id, lease_token)
    if job is None:
        return False
    job.state = "sent"
    job.sent_at = _now(now)
    job.lease_token = None
    job.lease_expires_at = None
    job.last_error_code = None
    return True


def finalize_oauth_followup_failure(session: Session, job_id: object, lease_token: object, *, category: object, retryable: bool, now: datetime | None = None) -> bool:
    job = _claimed_job(session, job_id, lease_token)
    if job is None:
        return False
    current = _now(now)
    code = _text(category)
    job.attempt_count += 1
    job.lease_token = None
    job.lease_expires_at = None
    job.last_error_code = code if code in RETRYABLE_ERROR_CODES | TERMINAL_ERROR_CODES else "followup_unavailable"
    if retryable and job.last_error_code in RETRYABLE_ERROR_CODES and job.attempt_count < MAX_ATTEMPTS:
        job.state = "pending"
        job.run_at = current + timedelta(minutes=2 ** job.attempt_count)
        return True
    job.state = "failed"
    job.failed_at = current
    return True


def reap_expired_oauth_followup_leases(session: Session, *, now: datetime | None = None) -> int:
    current = _now(now)
    jobs = list(session.scalars(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.state == "processing",
        InstagramOAuthFollowupJob.lease_expires_at.is_not(None),
        InstagramOAuthFollowupJob.lease_expires_at <= current,
    ).with_for_update(skip_locked=True)).all())
    for job in jobs:
        job.state = "failed"
        job.failed_at = current
        job.last_error_code = "lease_expired"
        job.lease_token = None
        job.lease_expires_at = None
    return len(jobs)


def oauth_followup_account_external_id(session: Session, instagram_account_id: object) -> str | None:
    """Load a trusted OAuth account identifier for a claimed scheduler job."""
    account_id = _uuid(instagram_account_id)
    if account_id is None:
        return None
    account = session.get(InstagramAccount, account_id)
    external_id = _text(account.instagram_user_id) if account is not None else ""
    return external_id or None


def _claimed_job(session: Session, job_id: object, lease_token: object) -> InstagramOAuthFollowupJob | None:
    identifier = _uuid(job_id)
    lease = _text(lease_token)
    if identifier is None or not lease:
        return None
    return session.scalar(select(InstagramOAuthFollowupJob).where(
        InstagramOAuthFollowupJob.id == identifier,
        InstagramOAuthFollowupJob.state == "processing",
        InstagramOAuthFollowupJob.lease_token == lease,
    ).with_for_update())
