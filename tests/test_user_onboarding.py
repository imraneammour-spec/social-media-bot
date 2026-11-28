"""Persisted customer-onboarding domain regression coverage."""

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.models import Base, User, UserOnboardingProfile
from database.onboarding import (
    MAIN_GOAL_VALUES,
    ONBOARDING_SOURCE_VALUES,
    PLAN_VALUES,
    USE_CASE_VALUES,
    OnboardingValidationError,
    apply_onboarding_step,
    expected_onboarding_step,
    onboarding_profile_for_user,
    user_onboarding_is_complete,
)


class UserOnboardingDomainTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.temp_dir.name) / 'user-onboarding.db'}"
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            self.profiled_user = User(email="new@example.test")
            self.legacy_user = User(email="legacy@example.test")
            session.add_all([self.profiled_user, self.legacy_user])
            session.flush()
            session.add(UserOnboardingProfile(user_id=self.profiled_user.id))

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_domain_uses_only_the_approved_stable_allowlists(self):
        self.assertEqual(
            USE_CASE_VALUES,
            (
                "personal_growth",
                "business_account",
                "agency_clients",
                "content_publishing",
                "comment_dm_automation",
                "other",
            ),
        )
        self.assertEqual(
            MAIN_GOAL_VALUES,
            (
                "save_time",
                "generate_leads",
                "increase_engagement",
                "consistent_posting",
                "multiple_accounts",
                "other",
            ),
        )
        self.assertEqual(
            ONBOARDING_SOURCE_VALUES,
            (
                "google",
                "instagram",
                "tiktok",
                "youtube",
                "friend_colleague",
                "github",
                "ai_tool",
                "other",
            ),
        )
        self.assertEqual(PLAN_VALUES, ("trial", "starter", "pro", "business"))

    def test_server_derives_each_step_from_persisted_state(self):
        with self.Session.begin() as session:
            profile = onboarding_profile_for_user(session, self.profiled_user.id)
            self.assertEqual(expected_onboarding_step(profile), "profile")

            apply_onboarding_step(
                profile,
                "profile",
                {"use_case": "business_account", "main_goal": "save_time"},
            )
            self.assertEqual(expected_onboarding_step(profile), "source")

            apply_onboarding_step(
                profile, "source", {"onboarding_source": "instagram"}
            )
            self.assertEqual(expected_onboarding_step(profile), "plan")

            completed_at = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
            apply_onboarding_step(
                profile, "plan", {"selected_plan": "pro"}, now=completed_at
            )
            self.assertEqual(profile.completed_at, completed_at)
            self.assertEqual(expected_onboarding_step(profile), "instagram")

    def test_invalid_or_out_of_order_step_does_not_mutate_profile(self):
        with self.Session.begin() as session:
            profile = onboarding_profile_for_user(session, self.profiled_user.id)
            before = (
                profile.use_case,
                profile.main_goal,
                profile.onboarding_source,
                profile.selected_plan,
                profile.completed_at,
            )
            with self.assertRaises(OnboardingValidationError):
                apply_onboarding_step(
                    profile, "source", {"onboarding_source": "google"}
                )
            self.assertEqual(
                (
                    profile.use_case,
                    profile.main_goal,
                    profile.onboarding_source,
                    profile.selected_plan,
                    profile.completed_at,
                ),
                before,
            )

            with self.assertRaises(OnboardingValidationError):
                apply_onboarding_step(
                    profile,
                    "profile",
                    {"use_case": "not-allowed", "main_goal": "save_time"},
                )
            self.assertEqual(profile.use_case, None)
            self.assertEqual(profile.main_goal, None)

    def test_profile_lookup_is_user_scoped_and_missing_row_is_legacy_complete(self):
        with self.Session() as session:
            self.assertIsNotNone(
                onboarding_profile_for_user(session, self.profiled_user.id)
            )
            self.assertFalse(
                user_onboarding_is_complete(session, self.profiled_user.id)
            )
            self.assertIsNone(
                onboarding_profile_for_user(session, self.legacy_user.id)
            )
            self.assertTrue(user_onboarding_is_complete(session, self.legacy_user.id))


if __name__ == "__main__":
    unittest.main()
