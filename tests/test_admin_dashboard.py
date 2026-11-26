"""Internal, read-only Postenza Admin dashboard regression coverage."""

import re
from datetime import datetime, timezone
import unittest

from sqlalchemy import func, select

from database.models import (
    BillingSubscription,
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

    def test_admin_link_is_hidden_for_customers_and_shown_to_the_internal_admin(self):
        """The /app/admin sidebar link is gated on internal admin access."""

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

    def _admin_csrf_token(self):
        page = self.client.get("/app/admin").get_data(as_text=True)
        return re.search(
            r'name="csrf_token" value="([^"]+)"', page
        ).group(1)

    def _add_accountless_user(self, email="solo@example.invalid"):
        with self.Session.begin() as session:
            user = User(email=email, display_name="Solo User")
            session.add(user)
            session.flush()
            user_id = user.id
        return user_id

    def test_admin_users_list_shows_plan_status_and_instagram_accounts(self):
        """The management table lists plan, status, and owned accounts per user."""

        with self.Session.begin() as session:
            session.add_all(
                [
                    UserOnboardingProfile(
                        user_id=self.owner_a.id,
                        selected_plan="free",
                    ),
                    UserOnboardingProfile(
                        user_id=self.owner_b.id,
                        selected_plan="pro",
                    ),
                ]
            )

        self._login_as(self.legacy_owner)

        page = self.client.get("/app/admin").get_data(as_text=True)

        self.assertEqual(page.count('data-admin-user="'), 3)
        self.assertIn('data-admin-user-plan="Free"', page)
        self.assertIn('data-admin-user-plan="Pro"', page)
        self.assertIn('data-admin-user-plan="Unknown / not provided"', page)
        self.assertIn('data-admin-user-status="active"', page)
        self.assertIn("alpha_first", page)
        self.assertIn("bravo_private", page)
        self.assertIn("legacy_private", page)
        self.assertIn("Current admin", page)

    def test_admin_can_disable_and_re_enable_a_user(self):
        """Disabling blocks sign-in; re-enabling restores it."""

        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()

        response = self.client.post(
            f"/app/admin/users/{self.owner_a.id}/status",
            data={"csrf_token": csrf_token, "status": "disabled"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/app/admin")
        with self.Session() as session:
            self.assertEqual(session.get(User, self.owner_a.id).status, "disabled")
        page = self.client.get("/app/admin").get_data(as_text=True)
        self.assertIn('data-admin-user-status="disabled"', page)

        # A disabled user is signed out at the next request.
        self._login_as(self.owner_a)
        signed_out = self.client.get("/app")
        self.assertEqual(signed_out.status_code, 302)
        self.assertIn("/login", signed_out.headers["Location"])

        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()
        response = self.client.post(
            f"/app/admin/users/{self.owner_a.id}/status",
            data={"csrf_token": csrf_token, "status": "active"},
        )
        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            self.assertEqual(session.get(User, self.owner_a.id).status, "active")

    def test_admin_cannot_change_their_own_status_or_delete_themselves(self):
        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()

        status_response = self.client.post(
            f"/app/admin/users/{self.legacy_owner.id}/status",
            data={"csrf_token": csrf_token, "status": "disabled"},
        )
        delete_response = self.client.post(
            f"/app/admin/users/{self.legacy_owner.id}/delete",
            data={"csrf_token": csrf_token, "confirm": "delete"},
        )

        self.assertEqual(status_response.status_code, 400)
        self.assertEqual(delete_response.status_code, 400)
        with self.Session() as session:
            self.assertEqual(
                session.get(User, self.legacy_owner.id).status, "active"
            )
            self.assertIsNotNone(session.get(User, self.legacy_owner.id))

    def test_admin_delete_needs_a_confirmation_step_then_deletes(self):
        """First delete POST shows a confirm step; only the confirming POST deletes."""

        solo_id = self._add_accountless_user()
        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()

        confirm_page = self.client.post(
            f"/app/admin/users/{solo_id}/delete",
            data={"csrf_token": csrf_token},
        )

        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn("Delete this user?", confirm_page.get_data(as_text=True))
        with self.Session() as session:
            self.assertIsNotNone(session.get(User, solo_id))

        response = self.client.post(
            f"/app/admin/users/{solo_id}/delete",
            data={"csrf_token": csrf_token, "confirm": "delete"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/app/admin")
        with self.Session() as session:
            self.assertIsNone(session.get(User, solo_id))

    def test_admin_cannot_delete_a_user_with_instagram_accounts(self):
        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()

        response = self.client.post(
            f"/app/admin/users/{self.owner_a.id}/delete",
            data={"csrf_token": csrf_token, "confirm": "delete"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Instagram accounts", response.get_data(as_text=True))
        with self.Session() as session:
            self.assertIsNotNone(session.get(User, self.owner_a.id))
            self.assertIsNotNone(
                session.get(InstagramAccount, self.account_a1.id)
            )

    def test_admin_cannot_delete_a_user_with_billing_records(self):
        solo_id = self._add_accountless_user()
        with self.Session.begin() as session:
            session.add(BillingSubscription(user_id=solo_id))
        self._login_as(self.legacy_owner)
        csrf_token = self._admin_csrf_token()

        response = self.client.post(
            f"/app/admin/users/{solo_id}/delete",
            data={"csrf_token": csrf_token, "confirm": "delete"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("billing records", response.get_data(as_text=True))
        with self.Session() as session:
            self.assertIsNotNone(session.get(User, solo_id))

    def test_non_admin_and_bad_token_cannot_change_users(self):
        """Admin POSTs need the legacy gate and a valid CSRF token."""

        self._login_as(self.owner_a)
        forbidden = self.client.post(
            f"/app/admin/users/{self.owner_b.id}/status",
            data={"csrf_token": "csrf-test-token", "status": "disabled"},
        )
        forbidden_delete = self.client.post(
            f"/app/admin/users/{self.owner_b.id}/delete",
            data={"csrf_token": "csrf-test-token", "confirm": "delete"},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden_delete.status_code, 403)
        with self.Session() as session:
            self.assertEqual(session.get(User, self.owner_b.id).status, "active")

        self._login_as(self.legacy_owner)
        bad_token = self.client.post(
            f"/app/admin/users/{self.owner_b.id}/status",
            data={"csrf_token": "wrong-token", "status": "disabled"},
        )
        unknown_user = self.client.post(
            "/app/admin/users/00000000-0000-0000-0000-000000000000/status",
            data={"csrf_token": self._admin_csrf_token(), "status": "disabled"},
        )
        invalid_status = self.client.post(
            f"/app/admin/users/{self.owner_b.id}/status",
            data={"csrf_token": self._admin_csrf_token(), "status": "banned"},
        )
        self.assertEqual(bad_token.status_code, 400)
        self.assertEqual(unknown_user.status_code, 404)
        self.assertEqual(invalid_status.status_code, 400)
        with self.Session() as session:
            self.assertEqual(session.get(User, self.owner_b.id).status, "active")

    def test_sidebar_has_billing_icon_and_back_link(self):
        """Billing keeps its link but gains an icon; the sidebar links home."""

        self._login_as(self.owner_a)
        # /app/automations 404s for an account with no automation config,
        # so assert the sidebar on the overview page instead.
        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn('href="/app/billing"', page)
        billing_link = re.search(
            r'<a href="/app/billing"[^>]*>(.*?)</a>', page
        ).group(0)
        self.assertIn("<svg", billing_link)
        self.assertIn('href="/"', page)
        self.assertIn("Back to Postenza", page)

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
