"""User-scoped persistence rules for the customer onboarding flow."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy.orm import Session

from .models import UserOnboardingProfile


USE_CASE_VALUES = (
    "personal_growth",
    "business_account",
    "agency_clients",
    "content_publishing",
    "comment_dm_automation",
    "other",
)
MAIN_GOAL_VALUES = (
    "save_time",
    "generate_leads",
    "increase_engagement",
    "consistent_posting",
    "multiple_accounts",
    "other",
)
ONBOARDING_SOURCE_VALUES = (
    "google",
    "instagram",
    "tiktok",
    "youtube",
    "friend_colleague",
    "github",
    "ai_tool",
    "other",
)
PLAN_VALUES = ("trial", "pro", "business")
# Legacy plan values stored before the v2 rename (same mapping as
# database/billing.py _LEGACY_PLAN_KEYS). Old rows are read as their v2
# equivalent so they keep working without a data migration.
LEGACY_PLAN_ALIASES = {"free": "trial", "agency": "business"}


def normalize_plan_value(value: object) -> str:
    """Map a legacy plan value to its v2 name, then validate it."""

    normalized = LEGACY_PLAN_ALIASES.get(str(value or "").strip(), str(value or "").strip())
    if normalized not in PLAN_VALUES:
        raise OnboardingValidationError("Invalid onboarding value for selected_plan.")
    return normalized


class OnboardingValidationError(ValueError):
    """The submitted onboarding transition is invalid or out of order."""


def onboarding_profile_for_user(
    session: Session, user_id: uuid.UUID
) -> UserOnboardingProfile | None:
    """Load only the profile whose primary key matches the authenticated user."""

    return session.get(UserOnboardingProfile, user_id)


def user_onboarding_is_complete(session: Session, user_id: uuid.UUID) -> bool:
    """Interpret a missing row as complete only for legacy-user compatibility."""

    profile = onboarding_profile_for_user(session, user_id)
    return profile is None or profile.completed_at is not None


def expected_onboarding_step(profile: UserOnboardingProfile) -> str:
    if not profile.use_case or not profile.main_goal:
        return "profile"
    if not profile.onboarding_source:
        return "source"
    if not profile.selected_plan or profile.completed_at is None:
        return "plan"
    return "instagram"


def _allowlisted(values: Mapping[str, object], key: str, allowed: tuple[str, ...]) -> str:
    value = str(values.get(key) or "").strip()
    if value not in allowed:
        raise OnboardingValidationError(f"Invalid onboarding value for {key}.")
    return value


def apply_onboarding_step(
    profile: UserOnboardingProfile,
    step: object,
    values: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> None:
    """Apply exactly the server-derived step after validating all of its values."""

    normalized_step = str(step or "").strip()
    if normalized_step != expected_onboarding_step(profile):
        raise OnboardingValidationError("Onboarding step is out of order.")

    if normalized_step == "profile":
        use_case = _allowlisted(values, "use_case", USE_CASE_VALUES)
        main_goal = _allowlisted(values, "main_goal", MAIN_GOAL_VALUES)
        profile.use_case = use_case
        profile.main_goal = main_goal
        return

    if normalized_step == "source":
        profile.onboarding_source = _allowlisted(
            values, "onboarding_source", ONBOARDING_SOURCE_VALUES
        )
        return

    if normalized_step == "plan":
        profile.selected_plan = normalize_plan_value(values.get("selected_plan"))
        profile.completed_at = now or datetime.now(timezone.utc)
        return

    raise OnboardingValidationError("Onboarding step cannot be updated.")
