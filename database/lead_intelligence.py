"""Safe, fact-only analysis helpers for one tenant-owned prompt request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import InstagramOAuthFollowupJob, InstagramPromptPost, InstagramPromptRequest


LEAD_INTENTS = (
    "interested",
    "asking_for_information",
    "prompt_request",
    "potential_customer",
    "unclear",
)
LEAD_PRIORITIES = ("low", "medium", "high")
MAX_AI_SUMMARY_LENGTH = 360
MAX_AI_SUGGESTED_FOLLOWUP_LENGTH = 280


@dataclass(frozen=True)
class LeadIntelligence:
    """A fully validated advisory analysis that is safe to persist."""

    intent: str
    priority: str
    summary: str
    suggested_followup: str

    @classmethod
    def safe_for_insufficient_context(cls) -> "LeadIntelligence":
        return cls(
            intent="unclear",
            priority="low",
            summary="Limited recorded interaction is available for this lead.",
            suggested_followup="Wait for another interaction before following up.",
        )


@dataclass(frozen=True)
class LeadIntelligenceContext:
    """Only non-identifying persisted interaction facts supplied to Gemini."""

    prompt_request_count: int
    source_post_available: bool
    prompts_prepared: bool
    followup_status: str

    def analysis_input(self) -> dict[str, int | bool | str]:
        return {
            "prompt_request_count": self.prompt_request_count,
            "source_post_available": self.source_post_available,
            "prompts_prepared": self.prompts_prepared,
            "followup_status": self.followup_status,
        }


def _followup_status(value: object) -> str:
    labels = {
        "pending": "scheduled",
        "processing": "scheduled",
        "sent": "sent",
        "cancelled": "cancelled",
        "failed": "unavailable",
    }
    return labels.get(str(value or "").strip().lower(), "none")


def lead_analysis_context(
    session: Session,
    *,
    lead: InstagramPromptRequest,
) -> LeadIntelligenceContext:
    """Build account-scoped facts without serializing identifiers or free text."""

    account_id = lead.instagram_account_id
    prompt_request_count = int(
        session.scalar(
            select(func.count())
            .select_from(InstagramPromptRequest)
            .where(
                InstagramPromptRequest.instagram_account_id == account_id,
                InstagramPromptRequest.recipient_id == lead.recipient_id,
            )
        )
        or 0
    )
    post = session.scalar(
        select(InstagramPromptPost).where(
            InstagramPromptPost.instagram_account_id == account_id,
            InstagramPromptPost.media_id == lead.media_id,
        )
    )
    followup_state = session.scalar(
        select(InstagramOAuthFollowupJob.state)
        .where(
            InstagramOAuthFollowupJob.instagram_account_id == account_id,
            InstagramOAuthFollowupJob.prompt_request_id == lead.id,
        )
        .order_by(
            InstagramOAuthFollowupJob.created_at.desc(),
            InstagramOAuthFollowupJob.id.desc(),
        )
        .limit(1)
    )
    return LeadIntelligenceContext(
        prompt_request_count=prompt_request_count,
        source_post_available=post is not None,
        prompts_prepared=bool(post and post.prompts),
        followup_status=_followup_status(followup_state),
    )


def _bounded_text(value: object, maximum: int) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text if 0 < len(text) <= maximum else None


def lead_intelligence_from_values(
    intent: object,
    priority: object,
    summary: object,
    suggested_followup: object,
) -> LeadIntelligence | None:
    """Return a complete finite advisory record, otherwise fail closed."""

    normalized_intent = str(intent or "").strip().lower()
    normalized_priority = str(priority or "").strip().lower()
    normalized_summary = _bounded_text(summary, MAX_AI_SUMMARY_LENGTH)
    normalized_followup = _bounded_text(
        suggested_followup, MAX_AI_SUGGESTED_FOLLOWUP_LENGTH
    )
    if (
        normalized_intent not in LEAD_INTENTS
        or normalized_priority not in LEAD_PRIORITIES
        or normalized_summary is None
        or normalized_followup is None
    ):
        return None
    return LeadIntelligence(
        intent=normalized_intent,
        priority=normalized_priority,
        summary=normalized_summary,
        suggested_followup=normalized_followup,
    )


def parse_lead_intelligence(text: object) -> LeadIntelligence | None:
    """Parse only a complete, finite Gemini JSON result without logging raw output."""

    source = str(text or "").strip()
    if source.startswith("```"):
        source = source.split("\n", 1)[-1]
        if source.endswith("```"):
            source = source[:-3].strip()
    try:
        payload = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return lead_intelligence_from_values(
        payload.get("intent"),
        payload.get("priority"),
        payload.get("summary"),
        payload.get("suggested_followup"),
    )


def lead_intelligence_prompt(context: LeadIntelligenceContext) -> str:
    """Build the bounded Gemini instruction from facts only."""

    facts = json.dumps(context.analysis_input(), sort_keys=True, separators=(",", ":"))
    return (
        "Analyze only these recorded, non-identifying Instagram lead facts. "
        "Do not infer budget, purchase intent, location, demographics, personal traits, "
        "or facts not present. If evidence is limited, use intent 'unclear' and priority 'low'. "
        "Return JSON only with intent, priority, summary, and suggested_followup. "
        "intent must be one of interested, asking_for_information, prompt_request, "
        "potential_customer, unclear. priority must be one of low, medium, high. "
        "summary and suggested_followup must each be short, practical, and non-pushy. "
        f"Facts: {facts}"
    )


def analyze_lead_intelligence(
    context: LeadIntelligenceContext,
    generate: Callable[[str], object],
) -> LeadIntelligence | None:
    """Generate a validated advisory result, with a deterministic low-evidence result."""

    if context.prompt_request_count < 1:
        return LeadIntelligence.safe_for_insufficient_context()
    return parse_lead_intelligence(generate(lead_intelligence_prompt(context)))
