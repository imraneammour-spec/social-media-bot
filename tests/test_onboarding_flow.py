"""End-to-end customer questionnaire behavior at the authenticated route."""

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import configure_auth, register_auth_routes
from database.models import Base, InstagramAccount, User, UserOnboardingProfile


class CustomerOnboardingFlowTest(unittest.TestCase):
    csrf_token = "onboarding-flow-csrf-token-with-32-byte-minimum"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = (
            f"sqlite:///{Path(self.temp_dir.name) / 'onboarding-flow.db'}"
        )
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = Flask(
            __name__,
            template_folder=str(
                Path(__file__).resolve().parents[1] / "dashboard" / "templates"
            ),
            static_folder=str(
                Path(__file__).resolve().parents[1] / "dashboard" / "static"
            ),
        )
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="onboarding-flow-test-secret",
            legacy_instagram_user_id="legacy-account-id",
            production=False,
        )
        register_auth_routes(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_user(self, email, *, profile=True, completed=False, connected=False):
        with self.Session.begin() as session:
            user = User(email=email, display_name=email.split("@", 1)[0])
            session.add(user)
            session.flush()
            if profile:
                session.add(
                    UserOnboardingProfile(
                        user_id=user.id,
                        use_case="business_account" if completed else None,
                        main_goal="save_time" if completed else None,
                        onboarding_source="google" if completed else None,
                        selected_plan="free" if completed else None,
                        completed_at=(
                            datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
                            if completed
                            else None
                        ),
                    )
                )
            if connected:
                session.add(
                    InstagramAccount(
                        user_id=user.id,
                        instagram_user_id=f"instagram-{email}",
                        username=email.split("@", 1)[0],
                        connection_status="connected",
                    )
                )
            return user.id

    def _login(self, user_id, *, locale="en"):
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
            browser_session["user_id"] = str(user_id)
            browser_session["_csrf_token"] = self.csrf_token
            browser_session["postenza_locale"] = locale

    def _post(self, **data):
        return self.client.post(
            "/onboarding",
            data={"csrf_token": self.csrf_token, **data},
            follow_redirects=False,
        )

    def test_new_profile_starts_at_server_controlled_profile_step(self):
        user_id = self._create_user("first-step@example.test")
        self._login(user_id)

        response = self.client.get("/onboarding")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Step 1 of 4", page)
        self.assertIn("Tell us about your Instagram work", page)
        self.assertIn('name="use_case"', page)
        self.assertIn('name="main_goal"', page)
        self.assertIn('name="step" value="profile"', page)
        self.assertNotIn("Connect Instagram", page)
        self.assertNotIn('name="user_id"', page)
        self.assertNotIn('name="account_id"', page)

    def test_valid_answers_advance_in_order_and_persist_only_their_step(self):
        user_id = self._create_user("ordered@example.test")
        self._login(user_id)

        profile_response = self._post(
            step="profile", use_case="agency_clients", main_goal="multiple_accounts"
        )
        self.assertEqual(profile_response.status_code, 303)
        source_page = self.client.get("/onboarding").get_data(as_text=True)
        self.assertIn("Step 2 of 4", source_page)
        self.assertIn("How did you hear about Postenza?", source_page)

        source_response = self._post(step="source", onboarding_source="friend_colleague")
        self.assertEqual(source_response.status_code, 303)
        plan_page = self.client.get("/onboarding").get_data(as_text=True)
        self.assertIn("Step 3 of 4", plan_page)
        self.assertEqual(plan_page.count("Pricing coming soon"), 3)
        self.assertIn("does not change your current access", plan_page)

        with self.Session() as session:
            profile = session.get(UserOnboardingProfile, user_id)
            self.assertEqual(profile.use_case, "agency_clients")
            self.assertEqual(profile.main_goal, "multiple_accounts")
            self.assertEqual(profile.onboarding_source, "friend_colleague")
            self.assertIsNone(profile.selected_plan)
            self.assertIsNone(profile.completed_at)

    def test_invalid_and_out_of_order_values_render_expected_step_without_mutation(self):
        user_id = self._create_user("invalid@example.test")
        self._login(user_id)

        invalid = self._post(
            step="profile", use_case="forged", main_goal="save_time"
        )
        self.assertEqual(invalid.status_code, 400)
        invalid_page = invalid.get_data(as_text=True)
        self.assertIn('class="alert alert-error auth-alert" role="alert"', invalid_page)
        self.assertIn("Choose one option for each question.", invalid_page)
        self.assertIn('value="save_time" checked', invalid_page)

        out_of_order = self._post(step="plan", selected_plan="agency")
        self.assertEqual(out_of_order.status_code, 400)
        self.assertIn("Tell us about your Instagram work", out_of_order.get_data(as_text=True))
        with self.Session() as session:
            profile = session.get(UserOnboardingProfile, user_id)
            self.assertIsNone(profile.use_case)
            self.assertIsNone(profile.main_goal)
            self.assertIsNone(profile.selected_plan)

    def test_onboarding_post_requires_csrf_and_does_not_mutate(self):
        user_id = self._create_user("csrf@example.test")
        self._login(user_id)

        response = self.client.post(
            "/onboarding",
            data={
                "step": "profile",
                "use_case": "personal_growth",
                "main_goal": "increase_engagement",
            },
        )

        self.assertEqual(response.status_code, 400)
        with self.Session() as session:
            profile = session.get(UserOnboardingProfile, user_id)
            self.assertIsNone(profile.use_case)
            self.assertIsNone(profile.main_goal)

    def test_plan_completion_is_informational_and_uses_connection_destination(self):
        for connected, expected_suffix in ((False, "/onboarding"), (True, "/app")):
            with self.subTest(connected=connected):
                user_id = self._create_user(
                    f"complete-{connected}@example.test", connected=connected
                )
                with self.Session.begin() as session:
                    profile = session.get(UserOnboardingProfile, user_id)
                    profile.use_case = "content_publishing"
                    profile.main_goal = "consistent_posting"
                    profile.onboarding_source = "youtube"
                self._login(user_id)

                response = self._post(step="plan", selected_plan="agency")

                self.assertEqual(response.status_code, 303)
                self.assertTrue(response.headers["Location"].endswith(expected_suffix))
                with self.Session() as session:
                    profile = session.get(UserOnboardingProfile, user_id)
                    self.assertEqual(profile.selected_plan, "agency")
                    self.assertIsNotNone(profile.completed_at)
                    user = session.get(User, user_id)
                    self.assertEqual(user.status, "active")

    def test_updates_are_scoped_to_authenticated_user(self):
        first_id = self._create_user("first@example.test")
        second_id = self._create_user("second@example.test")
        self._login(first_id)

        response = self._post(
            step="profile",
            use_case="personal_growth",
            main_goal="increase_engagement",
            user_id=str(second_id),
        )

        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            first = session.get(UserOnboardingProfile, first_id)
            second = session.get(UserOnboardingProfile, second_id)
            self.assertEqual(first.use_case, "personal_growth")
            self.assertEqual(first.main_goal, "increase_engagement")
            self.assertIsNone(second.use_case)
            self.assertIsNone(second.main_goal)

    def test_legacy_missing_profile_and_completed_profile_render_instagram_step(self):
        for profile in (False, True):
            with self.subTest(profile=profile):
                user_id = self._create_user(
                    f"instagram-step-{profile}@example.test",
                    profile=profile,
                    completed=profile,
                )
                self._login(user_id)
                page = self.client.get("/onboarding").get_data(as_text=True)
                self.assertIn("Step 4 of 4", page)
                self.assertIn("No Instagram account connected", page)
                self.assertIn("Connect Instagram", page)

    def test_arabic_onboarding_uses_rtl_and_localized_questionnaire_copy(self):
        user_id = self._create_user("arabic@example.test")
        self._login(user_id, locale="ar")

        page = self.client.get("/onboarding").get_data(as_text=True)

        self.assertIn('<html lang="ar" dir="rtl">', page)
        self.assertNotIn("Tell us about your Instagram work", page)
        self.assertIn('name="use_case"', page)


if __name__ == "__main__":
    unittest.main()
