"""Merged /app/engagement view: one route, old routes redirect to it."""

import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import (
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from database.models import (
    Base,
    InstagramAccount,
    InstagramCredential,
    InstagramEngagementEvent,
    InstagramOAuthFollowupJob,
    InstagramPromptPost,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
    User,
)
from tenant_dashboard import register_tenant_dashboard


class EngagementMergeTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'engagement.db'}"
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
            secret_key="engagement-merge-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "legacy dashboard")
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
            self.owner = User(email="owner@example.invalid", display_name="Owner")
            session.add(self.owner)
            session.flush()
            self.account = InstagramAccount(
                user_id=self.owner.id,
                instagram_user_id="10000000000000001",
                username="alpha_first",
                connection_status="connected",
                created_at=now - timedelta(minutes=2),
            )
            session.add(self.account)
            session.flush()
            session.add(InstagramCredential(
                instagram_account_id=self.account.id,
                token_ciphertext="ciphertext-a-secret",
                encryption_key_id="KEY_2026_01",
                scopes=["instagram_business_basic"],
                token_status="valid",
            ))
            session.add(InstagramPromptPost(
                instagram_account_id=self.account.id,
                media_id="media-a-secret",
                public_id="opaque-prompt-a",
                prompts=[{"text": "A prompt"}],
            ))
            session.add(InstagramPromptRequest(
                instagram_account_id=self.account.id,
                media_id="media-a-secret",
                comment_id="comment-a-secret",
                recipient_id="recipient-shared-secret",
                username="lead_alpha",
                page_url="https://internal.example.invalid/a",
            ))
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=self.account.id,
                comment_id="activity-a-secret",
                recipient_id="recipient-shared-secret",
                username="lead_alpha",
                reply_text="private reply content must not render",
            ))
            session.add(InstagramEngagementEvent(
                instagram_account_id=self.account.id,
                provider_event_id="provider-event-a-secret",
                event_type="message",
                state="done",
            ))
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=self.account.id,
                source_message_id="source-message-a-secret",
                recipient_id="recipient-shared-secret",
                page_url="https://internal.example.invalid/a",
                state="pending",
                run_at=now + timedelta(hours=6),
                lease_token="lease-a-secret",
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

    def test_merged_engagement_page_loads_with_leads_section(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        response = self.client.get(f"/app/engagement?account={selected}")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn('class="engagement-tabs"', page)
        self.assertIn("lead_alpha", page)
        self.assertIn("Managed lead records", page)

    def test_merged_engagement_sections_render_each_view(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        expectations = {
            "messages": "Message activity",
            "activity": "Engagement activity",
            "followups": "Follow-up activity",
        }
        for section, heading in expectations.items():
            page = self.client.get(
                f"/app/engagement?account={selected}&section={section}"
            ).get_data(as_text=True)
            self.assertIn(heading, page, section)

    def test_old_engagement_routes_redirect_to_merged_view(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        for path, section in (
            ("leads", "leads"),
            ("messages", "messages"),
            ("activity", "activity"),
            ("followups", "followups"),
        ):
            response = self.client.get(f"/app/{path}?account={selected}")
            self.assertEqual(response.status_code, 302, path)
            location = response.headers["Location"]
            self.assertTrue(location.startswith("/app/engagement"), location)
            self.assertIn(f"section={section}", location)

    def test_no_accounts_user_lands_on_onboarding_without_dashboard_loop(self):
        # The legacy /dashboard route now redirects to /app, so a
        # no-accounts user must not be sent there from /app (redirect loop).
        with self.Session.begin() as session:
            accountless = User(
                email="no-account@example.invalid", display_name="No Account"
            )
            session.add(accountless)
            session.flush()
        self._login_as(accountless)
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/onboarding"),
            response.headers["Location"],
        )
        followed = self.client.get("/app", follow_redirects=True)
        self.assertEqual(followed.status_code, 200)
        self.assertIn("onboarding", followed.request.path)

    def test_sidebar_links_to_merged_engagement_view(self):
        self._login_as(self.owner)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertRegex(page, r'href="/app/engagement\?account=[^"]*"')
        self.assertIn('aria-current="page"', page)
        self.assertNotIn('href="/app/leads?', page)

    def test_engagement_api_endpoints_still_serve(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        for path in ("leads", "activity", "followups", "messages"):
            response = self.client.get(f"/app/api/{path}?account={selected}")
            self.assertEqual(response.status_code, 200, path)


if __name__ == "__main__":
    unittest.main()
