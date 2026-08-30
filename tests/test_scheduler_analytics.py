"""Phase 5D — Scheduler & Analytics dashboard tests.

Verifies account-scoped tenant isolation, filters, pagination, empty states,
time ranges, and safe-fail behaviour for the Scheduler and Analytics screens.
"""

import tempfile
import unittest
import uuid
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import (
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from database import tenant_dashboard as tenant_data
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    InstagramEngagementEvent,
    InstagramOAuthFollowupJob,
    InstagramPromptPost,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
    User,
)
from tenant_dashboard import register_tenant_dashboard


class SchedulerAnalyticsDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase5d.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        template_dir = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        static_dir = Path(__file__).resolve().parents[1] / "dashboard" / "static"
        self.app = Flask(
            __name__,
            template_folder=str(template_dir),
            static_folder=str(static_dir),
        )
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="phase5d-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "legacy dashboard")
        self.app.add_url_rule("/api/legacy", "legacy_api", lambda: {"legacy": True})
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_tenant_dashboard(self.app)
        self._seed()
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            self.owner_a = User(email="owner-a@example.invalid", display_name="Owner A")
            self.owner_b = User(email="owner-b@example.invalid", display_name="Owner B")
            session.add_all([self.owner_a, self.owner_b])
            session.flush()
            self.account_a = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000001",
                username="alpha_account",
                connection_status="connected",
            )
            self.account_b = InstagramAccount(
                user_id=self.owner_b.id,
                instagram_user_id="20000000000000002",
                username="bravo_account",
                connection_status="connected",
            )
            session.add_all([self.account_a, self.account_b])
            session.flush()
            session.add_all([
                InstagramCredential(
                    instagram_account_id=self.account_a.id,
                    token_ciphertext="ciphertext-a-secret",
                    encryption_key_id="KEY_2026_01",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                    token_status="valid",
                ),
                InstagramCredential(
                    instagram_account_id=self.account_b.id,
                    token_ciphertext="ciphertext-b-secret",
                    encryption_key_id="KEY_2026_01",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                    token_status="valid",
                ),
            ])

            # Automation configs so /app/automations works
            session.add_all([
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a.id,
                    enabled=True,
                    trigger_keywords=["prompt"],
                    public_reply_enabled=False,
                    public_reply_text="",
                ),
                InstagramAutomationConfig(
                    instagram_account_id=self.account_b.id,
                    enabled=False,
                    trigger_keywords=[],
                    public_reply_enabled=False,
                    public_reply_text="",
                ),
            ])

            # Prompt posts — account A
            session.add_all([
                InstagramPromptPost(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-1-secret",
                    public_id="opaque-prompt-a1",
                    prompts=[{"text": "Prompt A1"}],
                    permalink="https://www.instagram.com/p/public-a1/",
                    cover_url="https://cdn.instagram.com/cover-a1.jpg",
                ),
                InstagramPromptPost(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-2-secret",
                    public_id="opaque-prompt-a2",
                    prompts=[],
                ),
            ])
            # Prompt post — account B
            session.add(InstagramPromptPost(
                instagram_account_id=self.account_b.id,
                media_id="media-b-1-secret",
                public_id="opaque-prompt-b1",
                prompts=[{"text": "Prompt B1"}],
            ))

            # Prompt requests — account A (2)
            session.add_all([
                InstagramPromptRequest(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-1-secret",
                    comment_id="comment-a-1-secret",
                    recipient_id="recipient-a-secret",
                    username="user_alpha_one",
                    page_url="https://internal.example.invalid/a1",
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-2-secret",
                    comment_id="comment-a-2-secret",
                    recipient_id="recipient-a2-secret",
                    username="user_alpha_two",
                    page_url="https://internal.example.invalid/a2",
                ),
            ])
            # Prompt request — account B (1)
            session.add(InstagramPromptRequest(
                instagram_account_id=self.account_b.id,
                media_id="media-b-1-secret",
                comment_id="comment-b-1-secret",
                recipient_id="recipient-b-secret",
                username="user_bravo_one",
                page_url="https://internal.example.invalid/b1",
            ))

            # Smart replies — account A
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=self.account_a.id,
                comment_id="smart-reply-a-secret",
                recipient_id="recipient-a-secret",
                username="user_alpha_one",
                reply_text="private smart reply a",
            ))
            # Smart reply — account B
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=self.account_b.id,
                comment_id="smart-reply-b-secret",
                recipient_id="recipient-b-secret",
                username="user_bravo_one",
                reply_text="private smart reply b",
            ))

            # Engagement events — account A
            session.add_all([
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a.id,
                    provider_event_id="event-a-comment-secret",
                    event_type="comment",
                    state="done",
                ),
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a.id,
                    provider_event_id="event-a-message-secret",
                    event_type="message",
                    state="done",
                ),
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a.id,
                    provider_event_id="event-a-failed-secret",
                    event_type="comment",
                    state="failed",
                    failed_at=now,
                ),
            ])
            # Engagement event — account B
            session.add(InstagramEngagementEvent(
                instagram_account_id=self.account_b.id,
                provider_event_id="event-b-comment-secret",
                event_type="comment",
                state="done",
            ))

            # Follow-up jobs — account A
            session.add_all([
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a.id,
                    source_message_id="followup-a-pending-secret",
                    recipient_id="recipient-a-secret",
                    page_url="https://internal.example.invalid/a-followup",
                    state="pending",
                    run_at=now + timedelta(hours=6),
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a.id,
                    source_message_id="followup-a-sent-secret",
                    recipient_id="recipient-a2-secret",
                    page_url="https://internal.example.invalid/a-followup2",
                    state="sent",
                    run_at=now,
                    sent_at=now,
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a.id,
                    source_message_id="followup-a-failed-secret",
                    recipient_id="recipient-a3-secret",
                    page_url="https://internal.example.invalid/a-followup3",
                    state="failed",
                    run_at=now - timedelta(hours=12),
                    failed_at=now - timedelta(hours=10),
                ),
            ])
            # Follow-up job — account B
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=self.account_b.id,
                source_message_id="followup-b-pending-secret",
                recipient_id="recipient-b-secret",
                page_url="https://internal.example.invalid/b-followup",
                state="pending",
                run_at=now + timedelta(hours=6),
            ))

    def _login_as(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = str(user.id)
            session["_csrf_token"] = "csrf-test-token"

    def _reference(self, user, account):
        return action_reference_for_account(
            user.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
        )

    def _scheduler_cancel_reference(self, user, account, source_message_id):
        with self.Session() as session:
            job = session.scalar(
                select(InstagramOAuthFollowupJob).where(
                    InstagramOAuthFollowupJob.instagram_account_id == account.id,
                    InstagramOAuthFollowupJob.source_message_id == source_message_id,
                )
            )
        return tenant_data.scheduler_cancel_reference_for_job(
            user.id,
            account.id,
            job.id,
            secret_key=self.app.secret_key,
        )

    # ==================================================================
    # SCHEDULER TESTS
    # ==================================================================

    def test_scheduler_selected_account_only_sees_own_jobs(self):
        """1. Selected account only sees its own scheduled jobs."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/scheduler?account={ref}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # Account A has 3 follow-up jobs
        self.assertEqual(len(data["jobs"]), 3)

    def test_scheduler_cross_account_jobs_never_appear(self):
        """2. Another account's jobs never appear."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/scheduler?account={ref}")
        data = response.get_json()
        rendered = str(data)
        self.assertNotIn("followup-b-pending-secret", rendered)
        self.assertNotIn("recipient-b-secret", rendered)

    def test_scheduler_status_filters_remain_account_scoped(self):
        """3. Status filters remain account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for status in ("all", "upcoming", "completed", "failed", "cancelled"):
            response = self.client.get(f"/app/api/scheduler?account={ref}&status={status}")
            self.assertEqual(response.status_code, 200)
            rendered = str(response.get_json())
            self.assertNotIn("followup-b-pending-secret", rendered)

    def test_scheduler_job_type_filters_remain_account_scoped(self):
        """4. Job-type filters remain account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for job_type in ("all", "followups"):
            response = self.client.get(f"/app/api/scheduler?account={ref}&job_type={job_type}")
            self.assertEqual(response.status_code, 200)
            rendered = str(response.get_json())
            self.assertNotIn("followup-b-pending-secret", rendered)

    def test_scheduler_pagination_remains_tenant_scoped(self):
        """5. Pagination remains tenant-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page1 = self.client.get(f"/app/api/scheduler?account={ref}&page=1").get_json()
        self.assertEqual(page1["pagination"]["page"], 1)
        self.assertNotIn("followup-b-pending-secret", str(page1))

    def test_scheduler_empty_state_works(self):
        """6. Empty state works."""
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            empty_account = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000077",
                username="empty_scheduler",
                connection_status="connected",
            )
            session.add(empty_account)
            session.flush()
            session.add(InstagramCredential(
                instagram_account_id=empty_account.id,
                token_ciphertext="ciphertext-empty-s",
                encryption_key_id="KEY_2026_01",
                scopes=["instagram_business_basic"],
                token_status="valid",
            ))
            empty_ref = action_reference_for_account(
                self.owner_a.id,
                empty_account.id,
                "tenant-dashboard-select",
                secret_key=self.app.secret_key,
            )
        response = self.client.get(f"/app/scheduler?account={empty_ref}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No scheduled jobs yet", response.get_data(as_text=True))

    def test_scheduler_invalid_account_fails_safely(self):
        """7. Stale/invalid account fails safely."""
        self._login_as(self.owner_a)
        response = self.client.get("/app/scheduler?account=malformed")
        self.assertEqual(response.status_code, 404)

    def test_scheduler_no_credentials_in_html(self):
        """8. No credentials/internal secrets appear in HTML."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/scheduler?account={ref}").get_data(as_text=True)
        for forbidden in (
            "ciphertext-a-secret",
            "KEY_2026_01",
            "10000000000000001",
            "recipient-a-secret",
            "followup-a-pending-secret",
            "https://internal.example.invalid/a-followup",
        ):
            self.assertNotIn(forbidden, page)
        with self.Session() as session:
            job = session.scalar(select(InstagramOAuthFollowupJob).where(
                InstagramOAuthFollowupJob.instagram_account_id == self.account_a.id,
                InstagramOAuthFollowupJob.source_message_id == "followup-a-pending-secret",
            ))
        api_data = self.client.get(f"/app/api/scheduler?account={ref}").get_json()
        self.assertNotIn(str(job.id), str(api_data))

    def test_scheduler_pending_job_can_be_cancelled_with_csrf_and_opaque_reference(self):
        """A selected owner's pending job can be cancelled without exposing IDs."""
        self._login_as(self.owner_a)
        account_ref = self._reference(self.owner_a, self.account_a)
        action_ref = self._scheduler_cancel_reference(
            self.owner_a, self.account_a, "followup-a-pending-secret"
        )
        page = self.client.get(f"/app/scheduler?account={account_ref}").get_data(as_text=True)
        self.assertIn("Cancel follow-up", page)
        self.assertNotIn("followup-a-pending-secret", page)
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(csrf_match)

        response = self.client.post(
            f"/app/scheduler/cancel?account={account_ref}",
            data={"csrf_token": csrf_match.group(1), "scheduler_action": action_ref},
        )
        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            job = session.scalar(select(InstagramOAuthFollowupJob).where(
                InstagramOAuthFollowupJob.instagram_account_id == self.account_a.id,
                InstagramOAuthFollowupJob.source_message_id == "followup-a-pending-secret",
            ))
            self.assertEqual(job.state, "cancelled")

    def test_scheduler_cancel_rejects_cross_account_action_without_mutating_job(self):
        """A cancel reference for a different owner's job cannot cross tenant scope."""
        self._login_as(self.owner_a)
        account_ref = self._reference(self.owner_a, self.account_a)
        foreign_action = self._scheduler_cancel_reference(
            self.owner_b, self.account_b, "followup-b-pending-secret"
        )

        response = self.client.post(
            f"/app/scheduler/cancel?account={account_ref}",
            data={"csrf_token": "csrf-test-token", "scheduler_action": foreign_action},
        )
        self.assertEqual(response.status_code, 404)
        with self.Session() as session:
            foreign_job = session.scalar(select(InstagramOAuthFollowupJob).where(
                InstagramOAuthFollowupJob.instagram_account_id == self.account_b.id,
                InstagramOAuthFollowupJob.source_message_id == "followup-b-pending-secret",
            ))
            self.assertEqual(foreign_job.state, "pending")

    def test_scheduler_cancel_requires_csrf(self):
        self._login_as(self.owner_a)
        account_ref = self._reference(self.owner_a, self.account_a)
        action_ref = self._scheduler_cancel_reference(
            self.owner_a, self.account_a, "followup-a-pending-secret"
        )

        response = self.client.post(
            f"/app/scheduler/cancel?account={account_ref}",
            data={"scheduler_action": action_ref},
        )
        self.assertEqual(response.status_code, 400)
        with self.Session() as session:
            job = session.scalar(select(InstagramOAuthFollowupJob).where(
                InstagramOAuthFollowupJob.instagram_account_id == self.account_a.id,
                InstagramOAuthFollowupJob.source_message_id == "followup-a-pending-secret",
            ))
            self.assertEqual(job.state, "pending")

    # ==================================================================
    # ANALYTICS TESTS
    # ==================================================================

    def test_analytics_metrics_only_count_selected_account(self):
        """11. Metrics only count selected account data."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        # Account A has 2 prompt requests
        self.assertEqual(data["metrics"]["prompt_requests"], 2)
        # Account A has 1 smart reply
        self.assertEqual(data["metrics"]["smart_replies_sent"], 1)
        # Account A has 2 comments handled
        self.assertEqual(data["metrics"]["comments_handled"], 2)
        # Account A has 1 message handled
        self.assertEqual(data["metrics"]["messages_handled"], 1)
        # Account A has 1 failed event
        self.assertEqual(data["metrics"]["failed_events"], 1)

    def test_analytics_cross_account_excluded(self):
        """12. Another account's activity is excluded."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}")
        data = response.get_json()
        # Account B has 1 prompt request, so account A should see 2, not 3
        self.assertEqual(data["metrics"]["prompt_requests"], 2)
        self.assertEqual(data["metrics"]["smart_replies_sent"], 1)
        # No cross-account data
        rendered = str(data)
        self.assertNotIn("user_bravo_one", rendered)
        self.assertNotIn("bravo_account", rendered)

    def test_analytics_7d_range_works(self):
        """13. 7d range works."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}&range=7d")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["range"], "7d")

    def test_analytics_30d_range_works(self):
        """14. 30d range works."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}&range=30d")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["range"], "30d")

    def test_analytics_all_time_range_works(self):
        """15. All-time range works."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}&range=all")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["range"], "all")

    def test_analytics_invalid_range_defaults_safely(self):
        """16. Invalid range fails safely / defaults to 30d."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/analytics?account={ref}&range=invalid")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["range"], "30d")

    def test_analytics_range_applies_to_leads_content_and_recent_failures(self):
        """The requested time range applies to every time-based V1 metric."""
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            old_request = InstagramPromptRequest(
                instagram_account_id=self.account_a.id,
                media_id="media-a-old-secret",
                comment_id="comment-a-old-secret",
                recipient_id="recipient-a-old-secret",
                username="user_alpha_old",
                page_url="https://internal.example.invalid/a-old",
            )
            old_post = InstagramPromptPost(
                instagram_account_id=self.account_a.id,
                media_id="media-a-old-content-secret",
                public_id="opaque-prompt-a-old",
                prompts=[{"text": "Old prompt"}],
            )
            old_failure = InstagramEngagementEvent(
                instagram_account_id=self.account_a.id,
                provider_event_id="event-a-old-failed-secret",
                event_type="comment",
                state="failed",
                failed_at=now - timedelta(days=31),
            )
            session.add_all([old_request, old_post, old_failure])
            session.flush()
            for record in (old_request, old_post, old_failure):
                record.created_at = now - timedelta(days=31)

        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        recent = self.client.get(f"/app/api/analytics?account={ref}&range=30d").get_json()
        all_time = self.client.get(f"/app/api/analytics?account={ref}&range=all").get_json()

        self.assertEqual(recent["metrics"]["leads_total"], 2)
        self.assertEqual(recent["metrics"]["prompt_enabled_content"], 1)
        self.assertEqual(len(recent["recent_failures"]), 1)
        self.assertEqual(all_time["metrics"]["leads_total"], 3)
        self.assertEqual(all_time["metrics"]["prompt_enabled_content"], 2)
        self.assertEqual(len(all_time["recent_failures"]), 2)

    def test_analytics_failed_events_counted_correctly(self):
        """17. Failed events counted correctly."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/analytics?account={ref}").get_json()
        self.assertEqual(data["metrics"]["failed_events"], 1)

    def test_analytics_prompt_requests_counted_correctly(self):
        """18. Prompt requests counted correctly."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/analytics?account={ref}").get_json()
        self.assertEqual(data["metrics"]["prompt_requests"], 2)

    def test_analytics_followup_states_counted_correctly(self):
        """19. Follow-up states counted correctly."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/analytics?account={ref}").get_json()
        # Account A: 1 pending, 0 processing, 1 sent, 0 cancelled, 1 failed
        self.assertEqual(data["followup_breakdown"]["scheduled"], 1)
        self.assertEqual(data["followup_breakdown"]["sent"], 1)
        self.assertEqual(data["followup_breakdown"]["failed"], 1)

    def test_analytics_content_counts_account_scoped(self):
        """20. Content counts are account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/analytics?account={ref}").get_json()
        # Account A has 2 prompt posts total, 1 with prompts
        self.assertEqual(data["metrics"]["total_content"], 2)
        self.assertEqual(data["metrics"]["prompt_enabled_content"], 1)

    def test_analytics_lead_metrics_account_scoped(self):
        """21. Lead metrics are account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/analytics?account={ref}").get_json()
        self.assertEqual(data["metrics"]["leads_total"], 2)
        self.assertEqual(data["metrics"]["leads_active"], 2)
        self.assertEqual(data["metrics"]["leads_archived"], 0)

    def test_analytics_no_credentials_in_html(self):
        """No credentials/internal secrets appear in Analytics HTML."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/analytics?account={ref}").get_data(as_text=True)
        for forbidden in (
            "ciphertext-a-secret",
            "KEY_2026_01",
            "10000000000000001",
            "recipient-a-secret",
            "comment-a-1-secret",
            "followup-a-pending-secret",
        ):
            self.assertNotIn(forbidden, page)

    # ==================================================================
    # NAVIGATION TESTS
    # ==================================================================

    def test_scheduler_link_enabled_in_sidebar(self):
        """22. Scheduler sidebar link enabled."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('href="/app/scheduler', page)
        self.assertNotIn(">Scheduler <small>Coming soon</small>", page)

    def test_analytics_link_enabled_in_sidebar(self):
        """23. Analytics sidebar link enabled."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('href="/app/analytics', page)
        self.assertNotIn(">Analytics <small>Coming soon</small>", page)

    def test_no_coming_soon_badges_for_scheduler_analytics(self):
        """24. No remaining Coming soon badges for implemented pages."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertNotIn("Coming soon", page)

    def test_account_switching_behavior_unchanged(self):
        """25. Account switching behavior unchanged."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn("alpha_account", page)

    def test_scheduler_active_navigation_marker(self):
        """Scheduler page shows active nav marker."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/scheduler?account={ref}").get_data(as_text=True)
        self.assertIn('aria-current="page">Scheduler', page)

    def test_analytics_active_navigation_marker(self):
        """Analytics page shows active nav marker."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/analytics?account={ref}").get_data(as_text=True)
        self.assertIn('aria-current="page">Analytics', page)

    # ==================================================================
    # REGRESSION — existing pages still work
    # ==================================================================

    def test_existing_dashboard_pages_still_work(self):
        """26-33. Home, Accounts, Automations, Leads, Activity, Follow-ups, Messages, Content."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for path in (
            "/app",
            f"/app/accounts?account={ref}",
            f"/app/automations?account={ref}",
            f"/app/leads?account={ref}",
            f"/app/activity?account={ref}",
            f"/app/followups?account={ref}",
            f"/app/messages?account={ref}",
            f"/app/content?account={ref}",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"Failed for {path}")

    def test_scheduler_page_renders(self):
        """Scheduler page renders correctly."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/scheduler?account={ref}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Scheduled jobs", page)
        self.assertIn("Follow-up", page)

    def test_analytics_page_renders(self):
        """Analytics page renders correctly."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/analytics?account={ref}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Comments handled", page)
        self.assertIn("Prompt requests", page)
        self.assertIn("Follow-up delivery", page)

    def test_scheduler_status_filter_upcoming(self):
        """Scheduler 'upcoming' filter shows only pending/processing jobs."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/scheduler?account={ref}&status=upcoming").get_json()
        statuses = {job["status"] for job in data["jobs"]}
        self.assertTrue(statuses.issubset({"Scheduled", "Processing"}))

    def test_scheduler_status_filter_completed(self):
        """Scheduler 'completed' filter shows only sent jobs."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/scheduler?account={ref}&status=completed").get_json()
        statuses = {job["status"] for job in data["jobs"]}
        self.assertTrue(statuses.issubset({"Sent"}))

    def test_scheduler_status_filter_failed(self):
        """Scheduler 'failed' filter shows only failed jobs."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        data = self.client.get(f"/app/api/scheduler?account={ref}&status=failed").get_json()
        statuses = {job["status"] for job in data["jobs"]}
        self.assertTrue(statuses.issubset({"Failed"}))


if __name__ == "__main__":
    unittest.main()
