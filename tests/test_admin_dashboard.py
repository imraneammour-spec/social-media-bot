"""Internal, read-only Postenza Admin dashboard regression coverage."""

from datetime import datetime, timezone
import unittest

from sqlalchemy import func, select

from database.models import (
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    User,
    UserOnboardingProfile,
)
from tests import test_tenant_dashboard as fixture


class AdminDashboardTest(unittest.TestCase):
    _seed = fixture.TenantDashboardTest._seed
    tearDown = fixture.TenantDashboardTest.tearDown

    def setUp(self):
        fixture.TenantDashboardTest.setUp(self)
        with self.Session.begin() as session:
            owner_a = session.get(User, self.owner_a.id)
            owner_b = session.get(User, self.owner_b.id)
            internal_owner = session.get(User, self.legacy_owner.id)
            owner_a.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            owner_b.created_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
            internal_owner.created_at = datetime(2026, 3, 1, tzinfo=timezone.utc)
            owner_a.password_hash = "private-password-hash"

            legacy_account = session.get(InstagramAccount, self.legacy_account.id)
            legacy_account.connection_status = "connected"

    def _login_as(self, user):
        with self.client.session_transaction() as session:
            session.clear()
            session["user_id"] = str(user.id)

    def test_internal_owner_can_access_admin_with_correct_read_only_metrics(self):
        """Removing the trusted owner check or aggregate filters breaks this."""

        self._login_as(self.legacy_owner)

        response = self.client.get("/app/admin")

        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Postenza Admin", page)
        self.assertIn('<dd data-admin-metric="registered-users">3</dd>', page)
        self.assertIn('<dd data-admin-metric="connected-instagram">4</dd>', page)
        self.assertIn('<dd data-admin-metric="active-users">Unavailable</dd>', page)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_normal_customer_and_forged_inputs_cannot_access_admin(self):
        """Client-controlled identity flags must never grant Admin capability."""

        self._login_as(self.owner_a)

        response = self.client.get(
            "/app/admin",
            query_string={
                "admin": "true",
                "user_id": str(self.legacy_owner.id),
                "legacy_instagram_user_id": self.legacy_account.instagram_user_id,
            },
        )
        forged_form = self.client.post(
            "/app/admin",
            data={
                "admin": "true",
                "user_id": str(self.legacy_owner.id),
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("registered-users", response.get_data(as_text=True))
        self.assertEqual(forged_form.status_code, 405)

    def test_admin_link_and_prompt_capability_share_internal_owner_boundary(self):
        """Admin visibility must track the established Prompt owner boundary."""

        self._login_as(self.owner_b)
        customer_page = self.client.get("/app/automations").get_data(as_text=True)
        self.assertNotIn('href="/app/admin"', customer_page)
        self.assertNotIn('value="prompt_request"', customer_page)

        with self.Session.begin() as session:
            legacy_account = session.get(InstagramAccount, self.legacy_account.id)
            legacy_account.user_id = self.owner_a.id
            session.add(
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a1.id,
                    delivery_mode="prompt_request",
                )
            )
        self._login_as(self.owner_a)
        internal_page = self.client.get("/app/automations").get_data(as_text=True)
        self.assertIn('href="/app/admin"', internal_page)
        self.assertIn('value="prompt_request"', internal_page)

    def test_recent_signups_are_newest_first_with_safe_account_counts(self):
        """A reversed query or tenant-scoped query breaks the internal signup list."""

        self._login_as(self.legacy_owner)

        page = self.client.get("/app/admin").get_data(as_text=True)

        self.assertLess(page.index("Legacy Owner"), page.index("Owner B"))
        self.assertLess(page.index("Owner B"), page.index("Owner A"))
        self.assertIn("legacy@example.invalid", page)
        self.assertIn("1 owned / 1 connected", page)
        self.assertIn("2 owned / 2 connected", page)

    def test_admin_never_renders_credentials_tokens_or_provider_secrets(self):
        """Joining credentials or operational payloads into the view breaks this."""

        self._login_as(self.legacy_owner)

        page = self.client.get("/app/admin").get_data(as_text=True)

        for secret in (
            "private-password-hash",
            "ciphertext-a-secret",
            "ciphertext-a2-secret",
            "ciphertext-b-secret",
            "KEY_2026_01",
            "KEY_2026_02",
            "provider-event-a-secret",
            "source-message-a-secret",
            "recipient-shared-secret",
        ):
            self.assertNotIn(secret, page)

    def test_plan_and_source_distributions_use_persisted_answers_and_unknown(self):
        """Every user must appear once without fabricating historical answers."""

        with self.Session.begin() as session:
            session.add_all(
                [
                    UserOnboardingProfile(
                        user_id=self.owner_a.id,
                        use_case="business_account",
                        main_goal="save_time",
                        onboarding_source="google",
                        selected_plan="free",
                        completed_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
                    ),
                    UserOnboardingProfile(
                        user_id=self.owner_b.id,
                        use_case="agency_clients",
                        main_goal="multiple_accounts",
                        onboarding_source="instagram",
                        selected_plan="pro",
                        completed_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
                    ),
                ]
            )

        self._login_as(self.legacy_owner)

        page = self.client.get("/app/admin").get_data(as_text=True)

        self.assertIn("Active users are unavailable until a persisted activity definition exists.", page)
        self.assertIn('<dd data-admin-plan="free">1</dd>', page)
        self.assertIn('<dd data-admin-plan="pro">1</dd>', page)
        self.assertIn('<dd data-admin-plan="agency">0</dd>', page)
        self.assertIn('<dd data-admin-plan="unknown">1</dd>', page)
        self.assertIn('<dd data-admin-source="google">1</dd>', page)
        self.assertIn('<dd data-admin-source="instagram">1</dd>', page)
        self.assertIn('<dd data-admin-source="unknown">1</dd>', page)
        self.assertEqual(page.count('data-admin-plan="'), 4)
        self.assertEqual(page.count('data-admin-source="'), 9)

    def test_unrecognized_persisted_answers_are_counted_as_unknown(self):
        with self.Session.begin() as session:
            session.add(
                UserOnboardingProfile(
                    user_id=self.owner_a.id,
                    onboarding_source="manual-unrecognized-source",
                    selected_plan="manual-unrecognized-plan",
                )
            )
        self._login_as(self.legacy_owner)

        page = self.client.get("/app/admin").get_data(as_text=True)

        self.assertIn('<dd data-admin-plan="unknown">3</dd>', page)
        self.assertIn('<dd data-admin-source="unknown">3</dd>', page)

    def test_admin_get_does_not_mutate_persisted_records(self):
        """Adding a write side effect to the Admin GET breaks this invariant."""

        def counts():
            with self.Session() as session:
                return (
                    session.scalar(select(func.count(User.id))),
                    session.scalar(select(func.count(InstagramAccount.id))),
                    session.scalar(select(func.count(InstagramCredential.id))),
                )

        before = counts()
        self._login_as(self.legacy_owner)

        response = self.client.get("/app/admin")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(counts(), before)


if __name__ == "__main__":
    unittest.main()
