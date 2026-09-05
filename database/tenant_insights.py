"""Read-only product summaries; never dispatch or mutate automation work."""
from sqlalchemy import func, select

from .models import InstagramOAuthDirectMessageInteraction


def direct_interactions(session, *, instagram_account_id, waiting_only=False, limit=25):
    query = select(InstagramOAuthDirectMessageInteraction).where(
        InstagramOAuthDirectMessageInteraction.instagram_account_id == instagram_account_id)
    if waiting_only:
        query = query.where(InstagramOAuthDirectMessageInteraction.recipient_reply_at.is_(None))
    rows = session.scalars(query.order_by(
        InstagramOAuthDirectMessageInteraction.initial_private_reply_at.desc(),
        InstagramOAuthDirectMessageInteraction.id.desc()).limit(max(1, min(limit, 100))))
    return [{
        "source_type": "Custom private message",
        "status": "Recipient replied" if row.recipient_reply_at else "Waiting for recipient reply",
        "created_at": row.initial_private_reply_at.isoformat(),
        "replied_at": row.recipient_reply_at.isoformat() if row.recipient_reply_at else None,
    } for row in rows]


def direct_metrics(session, *, instagram_account_id, since=None):
    model = InstagramOAuthDirectMessageInteraction
    def count(timestamp):
        query = select(func.count()).select_from(model).where(
            model.instagram_account_id == instagram_account_id, timestamp.is_not(None))
        if since is not None:
            query = query.where(timestamp >= since)
        return int(session.scalar(query) or 0)
    return {"direct_private_replies": count(model.initial_private_reply_at),
            "qualifying_recipient_replies": count(model.recipient_reply_at)}


def followup_presentation(job):
    """Translate only allowlisted state facts, never provider errors or message text."""
    status = {"pending": "Scheduled", "processing": "Processing", "sent": "Sent",
              "cancelled": "Cancelled", "failed": "Failed"}.get(job.state, "Unavailable")
    explanation = ""
    if job.state == "failed":
        if job.last_error_code == "outside_provider_window":
            status = "Expired"
            explanation = "The reply window closed before delivery. No message was sent."
        else:
            explanation = "Delivery could not be completed. Review your Instagram connection."
    elif job.state == "cancelled":
        explanation = "This follow-up was cancelled."
    completed_at = job.sent_at or job.failed_at or job.cancelled_at
    return {"status": status, "source_type": "Custom private message" if job.message_text else "Prompt delivery",
            "explanation": explanation,
            "scheduled_at": job.run_at.isoformat() if job.run_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "completed_at": completed_at.isoformat() if completed_at else None}
