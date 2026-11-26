"""Phase 6B account-scoped lead intelligence regressions."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from auth import (
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from database.lead_intelligence import (
    LeadIntelligence,
    LeadIntelligenceContext,
    lead_analysis_context,
    parse_lead_intelligence,
)
from database.models import (
    Base,
    InstagramAccount,
    InstagramOAuthFollowupJob,
    InstagramPromptPost,
    InstagramPromptRequest,
    User,
)
from tenant_dashboard import (
    lead_action_reference_for_prompt_request,
    register_tenant_dashboard,
)


class LeadIntelligenceTest(unittest.TestCase):
    CSRF_TOKEN = "csrf-lead-intelligence-test-token-with-sufficient-entropy"

    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'lead-intelligence.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        templates = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        self.app = Flask(__name__, template_folder=str(templates))
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="lead-intelligence-test-secret",
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
            self.owner_a = User(email="owner-a@example.invalid")
            self.owner_b = User(email="owner-b@example.invalid")
            session.add_all([self.owner_a, self.owner_b])
            session.flush()
            self.account_a = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000001",
                username="account_private_a",
                connection_status="connected",
            )
            self.account_b = InstagramAccount(
                user_id=self.owner_b.id,
                instagram_user_id="20000000000000001",
                username="account_private_b",
                connection_status="connected",
            )
            session.add_all([self.account_a, self.account_b])
            session.flush()
            self.lead_a = InstagramPromptRequest(
                instagram_account_id=self.account_a.id,
                media_id="media-private-a",
                comment_id="comment-private-a",
                recipient_id="recipient-private-shared",
                username="lead_private_a",
                page_url="https://example.invalid/p/o/a",
                internal_note="do not send this to Gemini",
                created_at=now,
            )
            self.lead_b = InstagramPromptRequest(
                instagram_account_id=self.account_b.id,
                media_id="media-private-b",
                comment_id="comment-private-b",
                recipient_id="recipient-private-shared",
                username="lead_private_b",
                page_url="https://example.invalid/p/o/b",
                created_at=now,
            )
            session.add_all([self.lead_a, self.lead_b])
            session.flush()
            session.add(InstagramPromptPost(
                instagram_account_id=self.account_a.id,
                media_id="media-private-a",
                public_id="opaque-public-a",
                prompts=[{"text": "prompt"}],
                permalink="https://www.instagram.com/p/example/",
            ))
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=self.account_a.id,
                prompt_request_id=self.lead_a.id,
                source_message_id="message-private-a",
                recipient_id="recipient-private-shared",
                page_url="https://example.invalid/p/o/a",
                state="pending",
                run_at=now,
            ))

    def _login_as_owner_a(self):
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = str(self.owner_a.id)
            browser_session["_csrf_token"] = self.CSRF_TOKEN

    def _account_reference(self, account):
        return action_reference_for_account(
            self.owner_a.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
        )

    def _analysis_reference(self, account, lead):
        return lead_action_reference_for_prompt_request(
            self.owner_a.id,
            account.id,
            lead.id,
            "lead-analysis",
            secret_key=self.app.secret_key,
        )

    def _lead(self, lead_id):
        with self.Session() as session:
            return session.get(InstagramPromptRequest, lead_id)

    def test_context_contains_only_safe_account_scoped_facts(self):
        with self.Session() as session:
            lead = session.get(InstagramPromptRequest, self.lead_a.id)
            context = lead_analysis_context(session, lead=lead)

        self.assertEqual(
            context.analysis_input(),
            {
                "prompt_request_count": 1,
                "source_post_available": True,
                "prompts_prepared": True,
                "followup_status": "scheduled",
            },
        )
        rendered = str(context.analysis_input())
        for forbidden in (
            "recipient-private-shared",
            "lead_private_a",
            "comment-private-a",
            "media-private-a",
            "do not send this to Gemini",
            "account_private_a",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_parser_rejects_invalid_ai_json_and_uses_safe_insufficient_context(self):
        self.assertIsNone(parse_lead_intelligence("not-json"))
        self.assertIsNone(parse_lead_intelligence(
            '{"intent":"invented","priority":"high","summary":"x","suggested_followup":"y"}'
        ))
        analysis = LeadIntelligence.safe_for_insufficient_context()
        self.assertEqual(analysis.intent, "unclear")
        self.assertEqual(analysis.priority, "low")

    def test_analyze_action_persists_valid_advisory_without_changing_manual_stage(self):
        self._login_as_owner_a()
        received_contexts = []

        def analyzer(context):
            received_contexts.append(context.analysis_input())
            return LeadIntelligence(
                intent="prompt_request",
                priority="medium",
                summary="Requested prepared prompts from one recorded post.",
                suggested_followup="Wait for another interaction before following up.",
            )

        self.app.extensions["lead_intelligence_analyzer"] = analyzer
        response = self.client.post(
            f"/app/leads/analyze?account={self._account_reference(self.account_a)}",
            data={
                "csrf_token": self.CSRF_TOKEN,
                "lead_action": self._analysis_reference(self.account_a, self.lead_a),
            },
        )

        self.assertEqual(response.status_code, 303)
        lead = self._lead(self.lead_a.id)
        self.assertEqual(lead.lead_status, "new")
        self.assertEqual(lead.ai_intent, "prompt_request")
        self.assertEqual(lead.ai_priority, "medium")
        self.assertEqual(lead.ai_summary, "Requested prepared prompts from one recorded post.")
        self.assertEqual(
            lead.ai_suggested_followup,
            "Wait for another interaction before following up.",
        )
        self.assertIsNotNone(lead.ai_analyzed_at)
        self.assertEqual(len(received_contexts), 1)

    def test_failed_or_invalid_reanalysis_preserves_previous_valid_intelligence(self):
        self._login_as_owner_a()
        with self.Session.begin() as session:
            lead = session.get(InstagramPromptRequest, self.lead_a.id)
            lead.ai_intent = "potential_customer"
            lead.ai_priority = "high"
            lead.ai_summary = "Previous safe summary."
            lead.ai_suggested_followup = "Review the source post before contacting."
            lead.ai_analyzed_at = datetime.now(timezone.utc)

        self.app.extensions["lead_intelligence_analyzer"] = lambda context: None
        response = self.client.post(
            f"/app/leads/analyze?account={self._account_reference(self.account_a)}",
            data={"csrf_token": self.CSRF_TOKEN, "lead_action": self._analysis_reference(self.account_a, self.lead_a)},
        )
        self.assertEqual(response.status_code, 303)
        lead = self._lead(self.lead_a.id)
        self.assertEqual(lead.ai_intent, "potential_customer")
        self.assertEqual(lead.ai_priority, "high")
        self.assertEqual(lead.ai_summary, "Previous safe summary.")

    def test_analysis_action_rejects_foreign_lead_and_requires_csrf(self):
        self._login_as_owner_a()
        calls = []
        self.app.extensions["lead_intelligence_analyzer"] = lambda context: calls.append(context)
        account_reference = self._account_reference(self.account_a)
        foreign_reference = self._analysis_reference(self.account_b, self.lead_b)
        foreign = self.client.post(
            f"/app/leads/analyze?account={account_reference}",
            data={"csrf_token": self.CSRF_TOKEN, "lead_action": foreign_reference},
        )
        csrf_failure = self.client.post(
            f"/app/leads/analyze?account={account_reference}",
            data={"lead_action": self._analysis_reference(self.account_a, self.lead_a)},
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(csrf_failure.status_code, 400)
        self.assertEqual(calls, [])

    def test_leads_render_not_analyzed_then_safe_intelligence_without_ai_on_get(self):
        self._login_as_owner_a()
        calls = []
        self.app.extensions["lead_intelligence_analyzer"] = lambda context: calls.append(context)
        url = f"/app/leads?account={self._account_reference(self.account_a)}"
        # /app/leads redirects to the merged engagement view (section=leads).
        before = self.client.get(url, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Not analyzed", before)
        self.assertIn("Analyze lead", before)
        self.assertEqual(calls, [])

        with self.Session.begin() as session:
            lead = session.get(InstagramPromptRequest, self.lead_a.id)
            lead.ai_intent = "prompt_request"
            lead.ai_priority = "medium"
            lead.ai_summary = "Safe summary <script>must be escaped</script>."
            lead.ai_suggested_followup = "Wait for another interaction."
            lead.ai_analyzed_at = datetime.now(timezone.utc)
        after = self.client.get(url, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Prompt Request", after)
        self.assertIn("Medium", after)
        self.assertIn("Safe summary &lt;script&gt;must be escaped&lt;/script&gt;.", after)
        self.assertIn("Wait for another interaction.", after)
        self.assertIn("Refresh analysis", after)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
