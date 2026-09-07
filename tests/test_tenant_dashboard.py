import inspect
import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
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
    InstagramAutomationConfig,
    InstagramCredential,
    InstagramContentItem,
    InstagramEngagementEvent,
    InstagramOAuthFollowupJob,
    InstagramPromptPost,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
    User,
)
from tenant_dashboard import register_tenant_dashboard


class TenantDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'tenant-dashboard.db'}"
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
            secret_key="tenant-dashboard-test-secret",
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
            self.legacy_owner = User(email="legacy@example.invalid", display_name="Legacy Owner")
            session.add_all([self.owner_a, self.owner_b, self.legacy_owner])
            session.flush()
            self.account_a1 = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000001",
                username="alpha_first",
                connection_status="connected",
                created_at=now - timedelta(minutes=2),
            )
            self.account_a2 = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000002",
                username="alpha_second",
                connection_status="connected",
                created_at=now - timedelta(minutes=1),
            )
            self.account_b = InstagramAccount(
                user_id=self.owner_b.id,
                instagram_user_id="20000000000000001",
                username="bravo_private",
                connection_status="connected",
            )
            self.legacy_account = InstagramAccount(
                user_id=self.legacy_owner.id,
                instagram_user_id="90000000000000001",
                username="legacy_private",
                connection_status="environment_managed",
            )
            session.add_all([self.account_a1, self.account_a2, self.account_b, self.legacy_account])
            session.flush()
            session.add_all([
                InstagramCredential(
                    instagram_account_id=self.account_a1.id,
                    token_ciphertext="ciphertext-a-secret",
                    encryption_key_id="KEY_2026_01",
                    scopes=["instagram_business_basic"],
                    token_status="valid",
                ),
                InstagramCredential(
                    instagram_account_id=self.account_a2.id,
                    token_ciphertext="ciphertext-a2-secret",
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
                    encryption_key_id="KEY_2026_02",
                    scopes=["instagram_business_basic"],
                    token_status="valid",
                ),
            ])
            session.add_all([
                InstagramPromptPost(
                    instagram_account_id=self.account_a1.id,
                    media_id="media-a-secret",
                    public_id="opaque-prompt-a",
                    prompts=[{"text": "A prompt"}],
                ),
                InstagramPromptPost(
                    instagram_account_id=self.account_b.id,
                    media_id="media-b-secret",
                    public_id="opaque-prompt-b",
                    prompts=[{"text": "B prompt"}],
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_a1.id,
                    media_id="media-a-secret",
                    comment_id="comment-a-secret",
                    recipient_id="recipient-shared-secret",
                    username="lead_alpha",
                    page_url="https://internal.example.invalid/a",
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_b.id,
                    media_id="media-b-secret",
                    comment_id="comment-b-secret",
                    recipient_id="recipient-shared-secret",
                    username="lead_bravo",
                    page_url="https://internal.example.invalid/b",
                ),
                InstagramSmartReplyActivity(
                    instagram_account_id=self.account_a1.id,
                    comment_id="activity-a-secret",
                    recipient_id="recipient-shared-secret",
                    username="lead_alpha",
                    reply_text="private reply content must not render",
                ),
                InstagramSmartReplyActivity(
                    instagram_account_id=self.account_b.id,
                    comment_id="activity-b-secret",
                    recipient_id="recipient-shared-secret",
                    username="lead_bravo",
                    reply_text="bravo private reply content",
                ),
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a1.id,
                    provider_event_id="provider-event-a-secret",
                    event_type="message",
                    state="done",
                ),
                InstagramEngagementEvent(
                    instagram_account_id=self.account_b.id,
                    provider_event_id="provider-event-b-secret",
                    event_type="comment",
                    state="failed",
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a1.id,
                    source_message_id="source-message-a-secret",
                    recipient_id="recipient-shared-secret",
                    page_url="https://internal.example.invalid/a",
                    state="pending",
                    run_at=now + timedelta(hours=6),
                    lease_token="lease-a-secret",
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_b.id,
                    source_message_id="source-message-b-secret",
                    recipient_id="recipient-shared-secret",
                    page_url="https://internal.example.invalid/b",
                    state="failed",
                    run_at=now,
                    last_error_code="provider-internal-secret",
                ),
            ])

    def _login_as(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = str(user.id)
            session["_csrf_token"] = "csrf-test-token"

    def _reference(self, user, account, *, now=None):
        return action_reference_for_account(
            user.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
            now=now,
        )

    def test_unauthenticated_and_legacy_requests_stay_outside_tenant_dashboard(self):
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        self._login_as(self.legacy_owner)
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/dashboard"))

        with self.Session.begin() as session:
            no_account_user = User(email="no-account@example.invalid", display_name="No Account")
            session.add(no_account_user)
            session.flush()
        self._login_as(no_account_user)
        response = self.client.get("/app")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))

    def test_default_switching_and_activation_status_are_owned_and_deterministic(self):
        self._login_as(self.owner_a)
        response = self.client.get("/app/api/accounts")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([item["username"] for item in payload["accounts"]], ["alpha_first", "alpha_second"])
        self.assertEqual(payload["accounts"][0]["activation_label"], "Reconnect required")
        self.assertEqual(payload["accounts"][1]["activation_label"], "Ready for activation")
        self.assertTrue(payload["accounts"][0]["is_selected"])

        selected = self._reference(self.owner_a, self.account_a2)
        response = self.client.get(f"/app/api/overview?account={selected}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["account"]["username"], "alpha_second")

    def test_connected_configured_legacy_account_is_visible_and_selectable_to_its_owner(self):
        """A connected OAuth account must not disappear just because its ID enables Prompt."""

        with self.Session.begin() as session:
            legacy = session.get(InstagramAccount, self.legacy_account.id)
            legacy.user_id = self.owner_a.id
            legacy.connection_status = "connected"
            session.add(InstagramAutomationConfig(
                instagram_account_id=legacy.id,
                delivery_mode="direct_message",
            ))

        self._login_as(self.owner_a)
        payload = self.client.get("/app/api/accounts").get_json()

        self.assertEqual(
            [item["username"] for item in payload["accounts"]],
            ["alpha_first", "alpha_second", "legacy_private"],
        )

        selected = self._reference(self.owner_a, self.legacy_account)
        response = self.client.get(f"/app/accounts?account={selected}")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Manage posts and automation for legacy_private.", page)
        self.assertIn("alpha_first", page)
        self.assertIn("alpha_second", page)
        self.assertIn("legacy_private", page)

        automation_response = self.client.get(
            f"/app/automations?account={selected}"
        )
        self.assertEqual(automation_response.status_code, 200)
        automation_page = automation_response.get_data(as_text=True)
        self.assertIn('value="prompt_request"', automation_page)

    def test_connected_configured_legacy_account_remains_tenant_isolated(self):
        """Another customer must neither list nor select an internal owner's accounts."""

        with self.Session.begin() as session:
            legacy = session.get(InstagramAccount, self.legacy_account.id)
            legacy.user_id = self.owner_a.id
            legacy.connection_status = "connected"
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_b.id,
                delivery_mode="direct_message",
            ))

        self._login_as(self.owner_b)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertIn("bravo_private", page)
        self.assertNotIn("alpha_first", page)
        self.assertNotIn("alpha_second", page)
        self.assertNotIn("legacy_private", page)

        foreign_reference = self._reference(self.owner_a, self.legacy_account)
        response = self.client.get(f"/app/accounts?account={foreign_reference}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json()["error"],
            "Selected account is unavailable.",
        )

        automation_response = self.client.get("/app/automations")
        self.assertEqual(automation_response.status_code, 200)
        automation_page = automation_response.get_data(as_text=True)
        self.assertNotIn('value="prompt_request"', automation_page)

    def test_environment_managed_legacy_account_remains_outside_tenant_listing(self):
        """The true environment-managed workspace stays separate from OAuth accounts."""

        with self.Session.begin() as session:
            legacy = session.get(InstagramAccount, self.legacy_account.id)
            legacy.user_id = self.owner_a.id
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                delivery_mode="direct_message",
            ))

        self._login_as(self.owner_a)
        payload = self.client.get("/app/api/accounts").get_json()

        self.assertEqual(
            [item["username"] for item in payload["accounts"]],
            ["alpha_first", "alpha_second"],
        )
        self.assertNotIn(
            "legacy_private",
            self.client.get("/app/accounts").get_data(as_text=True),
        )

        selected = self._reference(self.owner_a, self.account_a1)
        automation_response = self.client.get(
            f"/app/automations?account={selected}"
        )
        self.assertEqual(automation_response.status_code, 200)
        automation_page = automation_response.get_data(as_text=True)
        self.assertIn('value="prompt_request"', automation_page)

    def test_dashboard_activation_labels_are_sanitized_for_active_and_reconnect_required_states(self):
        self._login_as(self.owner_a)

        with patch.dict("os.environ", {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False):
            payload = self.client.get("/app/api/accounts").get_json()

        self.assertEqual(
            [item["activation_label"] for item in payload["accounts"]],
            ["Reconnect required", "Active"],
        )
        rendered = str(payload)
        for secret_or_internal in (
            "instagram_business_manage_comments",
            "instagram_business_manage_messages",
            "ciphertext-a-secret",
            "KEY_2026_01",
            "10000000000000001",
        ):
            self.assertNotIn(secret_or_internal, rendered)

    def test_accounts_hides_deployment_readiness_while_showing_active_account_state(self):
        self._login_as(self.owner_a)
        key = Fernet.generate_key().decode("ascii")
        environment = {
            "INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true",
            "PUBLIC_BASE_URL": "https://staging.example",
            "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "KEY_2026_01",
            "INSTAGRAM_CREDENTIAL_KEY_KEY_2026_01": key,
        }
        with patch.dict("os.environ", environment, clear=False):
            page = self.client.get("/app/accounts").get_data(as_text=True)
        self.assertIn("Your Instagram accounts", page)
        self.assertIn("Active", page)
        self.assertNotIn("Deployment readiness", page)
        self.assertNotIn(key, page)

    def test_accounts_hides_degraded_deployment_details(self):
        self._login_as(self.owner_a)
        with patch.dict("os.environ", {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true", "PUBLIC_BASE_URL": "http://unsafe.example"}, clear=False):
            page = self.client.get("/app/accounts").get_data(as_text=True)
        self.assertIn("Your Instagram accounts", page)
        self.assertNotIn("Deployment readiness", page)
        self.assertNotIn("http://unsafe.example", page)

    def test_selection_failures_are_generic_and_do_not_cross_tenants(self):
        self._login_as(self.owner_a)
        references = [
            "malformed",
            self._reference(self.owner_b, self.account_b),
            self._reference(self.owner_a, InstagramAccount(id=uuid.uuid4()), now=0),
            self._reference(self.owner_a, InstagramAccount(id=uuid.uuid4())),
        ]
        responses = [self.client.get(f"/app/api/overview?account={reference}") for reference in references]
        self.assertEqual([response.status_code for response in responses], [404, 404, 404, 404])
        self.assertEqual({response.get_json()["error"] for response in responses}, {"Selected account is unavailable."})

    def test_accounts_reject_cross_owner_account_reference(self):
        self._login_as(self.owner_a)
        foreign_reference = self._reference(self.owner_b, self.account_b)

        response = self.client.get(f"/app/accounts?account={foreign_reference}")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Selected account is unavailable.")

        owner_a_page = self.client.get("/app/accounts").get_data(as_text=True)
        owner_a_disconnect_action = re.search(
            r'name="account_action" value="([^"]+)"',
            owner_a_page,
        )
        self.assertIsNotNone(owner_a_disconnect_action)

        self._login_as(self.owner_b)
        owner_b_page = self.client.get("/app/accounts").get_data(as_text=True)
        owner_b_csrf_token = re.search(
            r'name="csrf_token" value="([^"]+)"',
            owner_b_page,
        )
        self.assertIsNotNone(owner_b_csrf_token)
        response = self.client.post(
            "/connect/instagram/disconnect",
            data={
                "csrf_token": owner_b_csrf_token.group(1),
                "account_action": owner_a_disconnect_action.group(1),
            },
        )

        self.assertEqual(response.status_code, 400)
        with self.Session() as session:
            self.assertEqual(
                session.get(InstagramAccount, self.account_a1.id).connection_status,
                "connected",
            )
            self.assertEqual(
                session.get(InstagramAccount, self.account_b.id).connection_status,
                "connected",
            )

    def test_accounts_disconnect_actions_are_not_exposed_by_account_api(self):
        self._login_as(self.owner_a)

        payload = self.client.get("/app/api/accounts").get_json()

        self.assertTrue(payload["accounts"])
        self.assertTrue(
            all("disconnect_action" not in account for account in payload["accounts"])
        )

    def test_accounts_show_reconnect_only_when_reconnect_is_needed(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a1.id)
            account.connection_status = "reconnect_required"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertEqual(page.count(">Reconnect Instagram</button>"), 1)
        self.assertIn('action="/connect/instagram"', page)

    def test_connected_account_missing_required_scopes_can_reconnect(self):
        """Capability-required reconnect must remain actionable for a connected row."""

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertIn("Reconnect required", page)
        self.assertEqual(page.count(">Reconnect Instagram</button>"), 1)
        self.assertIn('action="/connect/instagram"', page)

    def test_connected_account_with_error_shows_reconnect(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a1.id)
            account.last_connection_error_code = "connection_failed"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertEqual(page.count(">Reconnect Instagram</button>"), 1)

    def test_accounts_show_disconnect_only_for_disconnectable_accounts(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a2.id)
            account.connection_status = "disconnected"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertEqual(page.count("Confirm disconnect"), 1)
        self.assertEqual(page.count('action="/connect/instagram/disconnect"'), 1)

    def test_accounts_state_changing_actions_require_csrf_tokens(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a1.id)
            account.connection_status = "reconnect_required"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)
        disconnect_reference = re.search(
            r'name="account_action" value="([^"]+)"',
            page,
        )

        self.assertIsNotNone(disconnect_reference)
        self.assertEqual(
            len(re.findall(r'name="csrf_token" value="[^"]+"', page)),
            5,
        )
        self.assertEqual(self.client.post("/connect/instagram").status_code, 400)
        self.assertEqual(
            self.client.post(
                "/connect/instagram/disconnect",
                data={"account_action": disconnect_reference.group(1)},
            ).status_code,
            400,
        )

    def test_account_scoped_feeds_and_html_never_leak_private_data(self):
        self._login_as(self.owner_a)
        html_responses = []
        for path in ("/app", "/app/accounts", "/app/leads", "/app/activity", "/app/followups", "/app/messages", "/app/content"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            html_responses.append(response)

        responses = {
            "overview": self.client.get("/app/api/overview"),
            "leads": self.client.get("/app/api/leads"),
            "activity": self.client.get("/app/api/activity"),
            "followups": self.client.get("/app/api/followups"),
        }
        for response in responses.values():
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual([item["username"] for item in responses["leads"].get_json()["leads"]], ["lead_alpha"])
        self.assertIn("Smart reply", {item["type"] for item in responses["activity"].get_json()["activity"]})
        self.assertEqual(responses["followups"].get_json()["followups"][0]["status"], "Scheduled")
        self.assertEqual(responses["leads"].get_json()["leads"][0]["prompt_url"], "/p/o/opaque-prompt-a")

        rendered = "\n".join(response.get_data(as_text=True) for response in responses.values())
        rendered += "\n".join(response.get_data(as_text=True) for response in html_responses)
        forbidden = [
            str(self.account_a1.id), str(self.account_a2.id), str(self.account_b.id),
            "10000000000000001", "20000000000000001", "bravo_private", "lead_bravo",
            "ciphertext-a-secret", "KEY_2026_01", "recipient-shared-secret",
            "media-a-secret", "comment-a-secret", "source-message-a-secret",
            "lease-a-secret", "provider-event-a-secret", "provider-internal-secret",
            "private reply content must not render", "https://internal.example.invalid/a",
        ]
        for value in forbidden:
            self.assertNotIn(value, rendered)

    def test_oauth_tenant_remains_blocked_from_legacy_workspace(self):
        self._login_as(self.owner_a)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/api/legacy").status_code, 403)

    def test_tenant_dashboard_modules_do_not_use_legacy_runtime_or_credentials(self):
        import database.tenant_dashboard as tenant_data
        import tenant_dashboard as tenant_routes

        source = inspect.getsource(tenant_data) + inspect.getsource(tenant_routes)
        for forbidden in (
            "INSTAGRAM_ACCESS_TOKEN",
            "INSTAGRAM_USER_ID",
            "load_decrypted",
            "decrypt_",
            "token_ciphertext",
            "requests.",
            "load_json",
            "save_json",
        ):
            self.assertNotIn(forbidden, source)
        self.assertNotIn("from webhook", source)
        self.assertNotIn("import webhook", source)

    def _add_polish_records(self):
        """Create a fixed, account-A-only dataset for Phase 5C behaviors."""

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            existing = session.scalar(
                select(InstagramPromptRequest).where(
                    InstagramPromptRequest.username == "lead_alpha"
                )
            )
            existing.created_at = now - timedelta(days=2)
            statuses = ("new", "contacted", "qualified", "won", "lost")
            for index in range(26):
                session.add(
                    InstagramPromptRequest(
                        instagram_account_id=self.account_a1.id,
                        media_id=f"phase5c-media-{index}",
                        comment_id=f"phase5c-comment-{index}",
                        recipient_id=f"phase5c-recipient-{index}",
                        username=f"search_me_{index:02d}",
                        page_url="https://example.invalid/phase5c",
                        lead_status=statuses[index % len(statuses)],
                        created_at=now - timedelta(minutes=index),
                    )
                )
            session.add(
                InstagramPromptRequest(
                    instagram_account_id=self.account_a1.id,
                    media_id="phase5c-archived-media",
                    comment_id="phase5c-archived-comment",
                    recipient_id="phase5c-archived-recipient",
                    username="archived_alpha",
                    page_url="https://example.invalid/phase5c-archived",
                    lead_status="qualified",
                    lead_archived_at=now,
                    created_at=now - timedelta(hours=1),
                )
            )
            for state in ("sent", "cancelled", "failed"):
                session.add(
                    InstagramOAuthFollowupJob(
                        instagram_account_id=self.account_a1.id,
                        source_message_id=f"phase5c-source-{state}",
                        recipient_id=f"phase5c-followup-{state}",
                        page_url="https://example.invalid/phase5c-followup",
                        state=state,
                        run_at=now,
                    )
                )

    def test_overview_aggregates_and_recent_activity_stay_with_selected_account(self):
        self._add_polish_records()
        self._login_as(self.owner_a)

        response = self.client.get("/app/api/overview")
        self.assertEqual(response.status_code, 200)
        overview = response.get_json()["overview"]
        self.assertIn("lead_stage_counts", overview)
        self.assertEqual(
            overview["lead_stage_counts"],
            {"new": 7, "contacted": 5, "qualified": 5, "won": 5, "lost": 5},
        )
        self.assertEqual(overview["archived_lead_count"], 1)
        self.assertEqual(
            overview["followup_state_counts"],
            {"scheduled": 1, "sending": 0, "sent": 1, "cancelled": 1, "unavailable": 1},
        )
        self.assertTrue(overview["recent_activity"])
        self.assertNotIn("lead_bravo", str(overview))

        selected_a2 = self._reference(self.owner_a, self.account_a2)
        account_two = self.client.get(f"/app/api/overview?account={selected_a2}").get_json()["overview"]
        self.assertEqual(account_two["lead_count"], 0)
        self.assertEqual(account_two["followup_count"], 0)

    def test_leads_search_sort_filters_and_pagination_are_bounded_and_account_scoped(self):
        self._add_polish_records()
        self._login_as(self.owner_a)
        account = self._reference(self.owner_a, self.account_a1)

        first_page = self.client.get(
            f"/app/api/leads?account={account}&q=search_me&sort=newest&page=1"
        ).get_json()
        self.assertIn("pagination", first_page)
        self.assertEqual(len(first_page["leads"]), 25)
        self.assertEqual(first_page["pagination"], {"page": 1, "has_previous": False, "has_next": True})
        self.assertEqual(first_page["leads"][0]["username"], "search_me_00")

        second_page = self.client.get(
            f"/app/api/leads?account={account}&q=search_me&sort=newest&page=2"
        ).get_json()
        self.assertEqual(len(second_page["leads"]), 1)
        self.assertEqual(second_page["pagination"], {"page": 2, "has_previous": True, "has_next": False})

        oldest = self.client.get(
            f"/app/api/leads?account={account}&sort=oldest&page=1"
        ).get_json()
        self.assertEqual(oldest["leads"][0]["username"], "lead_alpha")

        qualified = self.client.get(
            f"/app/api/leads?account={account}&stage=qualified&view=active"
        ).get_json()
        self.assertEqual([lead["base_stage"] for lead in qualified["leads"]], ["qualified"] * 5)

        invalid = self.client.get(
            f"/app/api/leads?account={account}&sort=provider-id&page=-900&q={'x' * 500}"
        ).get_json()
        self.assertEqual(invalid["filters"], {"view": "active", "stage": None, "q": "x" * 80, "sort": "newest"})
        self.assertEqual(invalid["pagination"]["page"], 1)
        self.assertEqual(invalid["leads"], [])

        bounded_page = self.client.get(
            f"/app/api/leads?account={account}&page=999999"
        ).get_json()
        self.assertEqual(bounded_page["pagination"]["page"], 1000)
        self.assertEqual(bounded_page["leads"], [])

        page = self.client.get(
            f"/app/leads?account={account}&q=search_me&sort=oldest&page=1"
        ).get_data(as_text=True)
        self.assertIn("q=search_me", page)
        self.assertIn("sort=oldest", page)
        self.assertIn('aria-current="page"', page)

    def test_leads_render_only_same_account_public_source_post_context(self):
        with self.Session.begin() as session:
            primary_post = session.scalar(
                select(InstagramPromptPost).where(
                    InstagramPromptPost.instagram_account_id == self.account_a1.id,
                    InstagramPromptPost.media_id == "media-a-secret",
                )
            )
            primary_post.permalink = "https://www.instagram.com/p/public-alpha/"
            primary_post.cover_url = "https://cdn.instagram.com/public-alpha.jpg"
            session.add_all([
                InstagramPromptPost(
                    instagram_account_id=self.account_a1.id,
                    media_id="media-partial-secret",
                    public_id="opaque-prompt-partial",
                    prompts=[{"text": "Partial prompt"}],
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_a1.id,
                    media_id="media-partial-secret",
                    comment_id="comment-partial-secret",
                    recipient_id="recipient-partial-secret",
                    username="lead_partial",
                    page_url="https://internal.example.invalid/partial",
                ),
                InstagramPromptPost(
                    instagram_account_id=self.account_b.id,
                    media_id="media-cross-account-secret",
                    public_id="opaque-prompt-cross-account",
                    prompts=[{"text": "Other account prompt"}],
                    permalink="https://www.instagram.com/p/cross-owner/",
                    cover_url="https://cdn.instagram.com/cross-owner.jpg",
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_a1.id,
                    media_id="media-cross-account-secret",
                    comment_id="comment-cross-account-secret",
                    recipient_id="recipient-cross-account-secret",
                    username="lead_cross_account",
                    page_url="https://internal.example.invalid/cross-account",
                ),
            ])

        self._login_as(self.owner_a)
        account = self._reference(self.owner_a, self.account_a1)
        payload = self.client.get(
            f"/app/api/leads?account={account}&view=all"
        ).get_json()["leads"]
        leads = {lead["username"]: lead for lead in payload}

        self.assertEqual(leads["lead_alpha"]["prompt_url"], "/p/o/opaque-prompt-a")
        self.assertEqual(
            leads["lead_alpha"].get("post_permalink"),
            "https://www.instagram.com/p/public-alpha/",
        )
        self.assertEqual(
            leads["lead_alpha"].get("cover_url"),
            "https://cdn.instagram.com/public-alpha.jpg",
        )
        self.assertEqual(leads["lead_partial"]["prompt_url"], "/p/o/opaque-prompt-partial")
        self.assertIsNone(leads["lead_partial"].get("post_permalink"))
        self.assertIsNone(leads["lead_partial"].get("cover_url"))
        self.assertIsNone(leads["lead_cross_account"]["prompt_url"])
        self.assertIsNone(leads["lead_cross_account"].get("post_permalink"))
        self.assertIsNone(leads["lead_cross_account"].get("cover_url"))

        page = self.client.get(
            f"/app/leads?account={account}&view=all"
        ).get_data(as_text=True)
        self.assertNotIn('href="/p/o/opaque-prompt-a"', page)
        self.assertIn('href="https://www.instagram.com/p/public-alpha/"', page)
        self.assertIn("View Instagram post", page)
        self.assertIn('src="https://cdn.instagram.com/public-alpha.jpg"', page)
        self.assertNotIn("https://www.instagram.com/p/cross-owner/", page)
        self.assertNotIn("https://cdn.instagram.com/cross-owner.jpg", page)
        for forbidden in (
            "media-a-secret",
            "media-partial-secret",
            "media-cross-account-secret",
            "comment-partial-secret",
            "recipient-partial-secret",
            "https://internal.example.invalid/partial",
            "https://internal.example.invalid/cross-account",
        ):
            self.assertNotIn(forbidden, page)

    def test_polished_pages_show_owned_accounts_and_sanitized_empty_activation_states(self):
        self._login_as(self.owner_a)
        account_a2 = self._reference(self.owner_a, self.account_a2)

        accounts = self.client.get("/app/accounts").get_data(as_text=True)
        self.assertIn("alpha_first", accounts)
        self.assertIn("alpha_second", accounts)
        self.assertNotIn("bravo_private", accounts)
        self.assertIn("Reconnect Instagram to restore automation access.", accounts)
        self.assertIn("Instagram is connected and ready, but engagement automation is not enabled yet.", accounts)

        for path, message in (
            (f"/app/leads?account={account_a2}", "No leads yet."),
            (f"/app/activity?account={account_a2}", "No activity yet."),
            (f"/app/followups?account={account_a2}", "No follow-ups yet."),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn(message, response.get_data(as_text=True))

    def test_activity_followups_are_scoped_filtered_normalized_and_sanitized(self):
        self._add_polish_records()
        self._login_as(self.owner_a)
        account = self._reference(self.owner_a, self.account_a1)

        response = self.client.get(
            f"/app/api/activity?account={account}&event_type=followups"
        )
        self.assertEqual(response.status_code, 200)
        activity = response.get_json()["activity"]
        self.assertEqual(len(activity), 4)
        self.assertEqual(
            {(item["type"], item["state"]) for item in activity},
            {
                ("Follow-up scheduled", "Waiting"),
                ("Follow-up sent", "Completed"),
                ("Follow-up cancelled", "Completed"),
                ("Follow-up failed", "Unavailable"),
            },
        )
        self.assertTrue(all(not item["username"] for item in activity))

        rendered = self.client.get(
            f"/app/activity?account={account}&event_type=followups"
        ).get_data(as_text=True)
        self.assertIn("Follow-ups", rendered)
        self.assertIn('value="followups" selected', rendered)
        for forbidden in (
            "source-message-a-secret",
            "recipient-shared-secret",
            "https://internal.example.invalid/a",
            "lease-a-secret",
            "provider-internal-secret",
            "phase5c-source-sent",
            "phase5c-followup-sent",
            "https://example.invalid/phase5c-followup",
        ):
            self.assertNotIn(forbidden, rendered)

        all_activity = self.client.get(
            f"/app/api/activity?account={account}&event_type=all"
        ).get_json()["activity"]
        all_types = {item["type"] for item in all_activity}
        self.assertIn("Comment interaction", all_types)
        self.assertIn("Smart reply", all_types)
        self.assertIn("Message received", all_types)
        self.assertIn("Follow-up scheduled", all_types)

    def test_database_failure_returns_one_generic_workspace_message(self):
        self._login_as(self.owner_a)
        auth_state = self.app.extensions["phase3a_auth"]

        with patch.object(
            auth_state,
            "session_factory",
            side_effect=SQLAlchemyError("private database details"),
        ):
            response = self.client.get("/app")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_data(as_text=True), "Workspace temporarily unavailable.")
        self.assertNotIn("private database details", response.get_data(as_text=True))

    def test_authenticated_oauth_user_sees_shell_with_safe_navigation(self):
        self._login_as(self.owner_a)

        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn('class="app-shell"', page)
        self.assertIn('class="app-sidebar"', page)
        self.assertIn('/static/tenant_dashboard.css', page)
        self.assertIn('aria-label="Primary navigation"', page)
        self.assertIn('aria-current="page"><svg', page)
        for label in ("Content", "Calendar", "Engagement", "Automation", "Instagram", "Analytics"):
            self.assertIn(f">{label}</a>", page)
        for legacy_label in ("Home", "Leads", "Messages", "Scheduler", "Accounts", "Activity", "Follow-ups"):
            self.assertNotIn(f">{legacy_label}</a>", page)
        self.assertIn('aria-label="Open navigation"', page)
        self.assertIn('action="/logout"', page)
        self.assertIn('>Sign out</button>', page)
        self.assertIn("Postenza", page)
        self.assertNotIn("Imran Prompts", page)
        self.assertNotIn('href="/dashboard', page)
        self.assertNotIn('href="/api/', page)

    def test_overview_prioritizes_connection_and_content_actions(self):
        self._login_as(self.owner_a)

        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn("Your Instagram connection", page)
        self.assertIn("Create image post", page)
        self.assertIn("Content at a glance", page)
        self.assertIn("Next scheduled post", page)
        self.assertIn("Automation is off", page)
        self.assertNotIn("Deployment readiness", page)

    def test_shell_active_navigation_remains_page_specific_and_opaque(self):
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a2)

        leads = self.client.get(f"/app/leads?account={selected}").get_data(as_text=True)
        accounts = self.client.get(f"/app/accounts?account={selected}").get_data(as_text=True)

        self.assertIn('aria-current="page"><svg aria-hidden="true"', leads)
        self.assertIn('</svg>Engagement</a>', leads)
        self.assertIn('aria-current="page"><svg aria-hidden="true"', accounts)
        self.assertIn('</svg>Instagram</a>', accounts)
        self.assertNotIn(str(self.account_a2.id), leads)
        self.assertNotIn(self.account_a2.instagram_user_id, leads)

    def test_account_switcher_keeps_the_current_dashboard_page(self):
        """Changing an owned account does not unexpectedly send a user to Home."""
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        other = self._reference(self.owner_a, self.account_a2)

        page = self.client.get(f"/app/leads?account={selected}").get_data(as_text=True)

        self.assertIn(f'href="/app/leads?account={other}"', page)
        self.assertNotIn(f'href="/app?account={other}"', page)

    def test_automations_uses_one_page_heading_and_labeled_checkboxes(self):
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["prompts"],
                public_reply_enabled=True,
                public_reply_text="Thanks",
            ))

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)

        self.assertIn("<title>Automation | Postenza</title>", page)
        self.assertIn('<h1>Automation</h1>', page)
        self.assertNotIn('<h1>Instagram workspace</h1>', page)
        self.assertIn('for="automation_enabled"', page)
        self.assertIn('for="message_public_reply_enabled"', page)
        self.assertIn('for="link_public_reply_enabled"', page)
        self.assertIn("Smart replies", page)
        self.assertIn('for="smart_reply_enabled"', page)
        self.assertIn('id="smart_reply_tone"', page)
        self.assertIn('id="smart_reply_max_per_hour"', page)

    def test_automations_updates_smart_reply_settings_only_for_selected_account(self):
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add_all([
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a1.id,
                    enabled=True,
                    trigger_keywords=["prompt"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    smart_reply_enabled=False,
                    smart_reply_tone="friendly",
                    smart_reply_max_per_hour=20,
                ),
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a2.id,
                    enabled=True,
                    trigger_keywords=["access"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    smart_reply_enabled=True,
                    smart_reply_tone="neutral",
                    smart_reply_max_per_hour=9,
                ),
            ])

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)
        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "trigger_keywords": "prompt",
                "public_reply_text": "",
                "smart_reply_enabled": "on",
                "smart_reply_tone": "professional",
                "smart_reply_max_per_hour": "7",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.Session() as session:
            primary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
            secondary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a2.id
            ))
        self.assertTrue(primary.smart_reply_enabled)
        self.assertEqual(primary.smart_reply_tone, "professional")
        self.assertEqual(primary.smart_reply_max_per_hour, 7)
        self.assertEqual(secondary.smart_reply_tone, "neutral")
        self.assertEqual(secondary.smart_reply_max_per_hour, 9)

        invalid = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "trigger_keywords": "prompt",
                "public_reply_text": "",
                "smart_reply_enabled": "on",
                "smart_reply_tone": "invalid-tone",
                "smart_reply_max_per_hour": "101",
            },
        )
        self.assertEqual(invalid.status_code, 400)
        with self.Session() as session:
            primary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
        self.assertEqual(primary.smart_reply_tone, "professional")
        self.assertEqual(primary.smart_reply_max_per_hour, 7)

    def test_home_renders_existing_sanitized_status_and_summary_data(self):
        self._login_as(self.owner_a)

        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn("Your Instagram connection", page)
        self.assertIn("Connected", page)
        self.assertIn("Automation is off", page)
        self.assertIn("Content at a glance", page)
        self.assertIn("Recent activity", page)
        self.assertNotIn("Instagram engagement automation is active.", page)
        for forbidden in (
            self.account_a1.instagram_user_id,
            "ciphertext-a-secret",
            "KEY_2026_01",
            "provider-event-a-secret",
        ):
            self.assertNotIn(forbidden, page)

        empty_account = self._reference(self.owner_a, self.account_a2)
        empty_page = self.client.get(f"/app?account={empty_account}").get_data(as_text=True)
        self.assertIn("Activity will appear here when your account is used.", empty_page)

    def test_tenant_dashboard_stylesheet_is_served_without_legacy_routes(self):
        response = self.client.get("/static/tenant_dashboard.css")

        self.assertEqual(response.status_code, 200)
        stylesheet = response.get_data(as_text=True)
        self.assertIn(".app-shell", stylesheet)
        self.assertIn("@media", stylesheet)
        self.assertIn('.mobile-nav-toggle', stylesheet)
        self.assertIn(":focus-visible", stylesheet)
        self.assertNotIn("/dashboard", stylesheet)
        self.assertNotIn("/api/", stylesheet)
        response.close()

    def test_desktop_workspace_uses_the_full_width_after_the_sidebar(self):
        """Large authenticated screens must not cap the workspace at document width."""
        response = self.client.get("/static/tenant_dashboard.css")
        stylesheet = response.get_data(as_text=True)

        self.assertIn(".app-main{width:calc(100% - 248px);", stylesheet)
        self.assertIn("@media(min-width:901px)", stylesheet)
        response.close()

    def test_sidebar_brand_uses_a_centered_readable_logo_width(self):
        """The sidebar logo must remain legible without widening the navigation."""
        response = self.client.get("/static/tenant_dashboard.css")
        stylesheet = response.get_data(as_text=True)

        self.assertIn(".app-brand{display:block;width:min(100%,196px);margin:4px auto 36px}", stylesheet)
        self.assertIn(".app-brand img{display:block;width:100%;height:auto}", stylesheet)
        response.close()

    def test_calendar_prioritizes_safe_publishing_agenda_states(self):
        """Removing a lifecycle section would hide a publishing outcome from its owner."""
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            session.add_all([
                InstagramContentItem(
                    instagram_account_id=self.account_a1.id,
                    status="scheduled",
                    content_type="image",
                    caption="Agenda scheduled image",
                    media_url="https://cdn.example.com/scheduled.jpg",
                    scheduled_for=now + timedelta(hours=1),
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_a1.id,
                    status="published",
                    content_type="image",
                    caption="Agenda published image",
                    media_url="https://cdn.example.com/published.jpg",
                    published_at=now - timedelta(hours=1),
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_a1.id,
                    status="failed",
                    content_type="image",
                    caption="Agenda needs review",
                    media_url="https://cdn.example.com/failed.jpg",
                    publish_error_code="provider_rejected",
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_b.id,
                    status="published",
                    content_type="image",
                    caption="Private foreign agenda image",
                    media_url="https://cdn.example.com/private.jpg",
                ),
            ])

        page = self.client.get(f"/app/scheduler?account={selected}").get_data(as_text=True)

        for label in ("Publishing agenda", "All", "Scheduled", "Published", "Needs attention"):
            self.assertIn(label, page)
        for caption in ("Agenda scheduled image", "Agenda published image", "Agenda needs review"):
            self.assertIn(caption, page)
        self.assertNotIn("Private foreign agenda image", page)
        self.assertNotIn("provider_rejected", page)

    def test_instagram_page_uses_safe_connection_cards_and_disconnect_confirmation(self):
        """A connection problem must be understandable without OAuth diagnostics."""
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a1.id)
            account.connection_status = "reconnect_required"

        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertIn("Your Instagram accounts", page)
        self.assertIn("Reconnect required", page)
        self.assertIn("Confirm disconnect", page)
        self.assertNotIn("Deployment readiness", page)
        self.assertNotIn("credential", page.lower())

    def test_automation_groups_existing_controls_by_public_private_and_smart_outcomes(self):
        """The same automation inputs remain available in outcome-oriented groups."""
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                public_reply_enabled=True,
                followup_enabled=True,
                smart_reply_enabled=True,
            ))
        page = self.client.get("/app/automations").get_data(as_text=True)

        for label in ("Automation status", "Reply to matching comments", "Private message and follow-up", "Smart replies"):
            self.assertIn(label, page)
        for field in (
            "automation_enabled",
            "message_public_reply_enabled",
            "message_followup_enabled",
            "smart_reply_enabled",
        ):
            self.assertIn(f'id="{field}"', page)
        self.assertIn("Public replies appear on your Instagram post.", page)
        self.assertNotIn("Required system step", page)

    def test_engagement_routes_share_accessible_tabs_without_sidebar_duplication(self):
        """Each Engagement view must provide the same route-preserving subnavigation."""
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)

        for path in ("leads", "messages", "activity", "followups"):
            page = self.client.get(f"/app/{path}?account={selected}").get_data(as_text=True)
            self.assertIn('class="engagement-tabs"', page)
            self.assertIn('aria-label="Engagement sections"', page)
            for label in ("Leads", "Message activity", "Activity", "Follow-ups"):
                self.assertIn(f">{label}</a>", page)

    def test_postenza_brand_assets_and_browser_identity_render_in_workspace(self):
        """A rebrand must update the visible authenticated product identity."""
        self._login_as(self.owner_a)

        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn("Postenza", page)
        self.assertIn("postenza-sidebar-logo-dark.png", page)
        self.assertIn("postenza-favicon.png", page)
        self.assertIn('sizes="16x16" href="/static/brand/postenza-favicon-16.png"', page)
        self.assertIn('sizes="32x32" href="/static/brand/postenza-favicon-32.png"', page)
        self.assertIn('sizes="48x48" href="/static/brand/postenza-favicon-48.png"', page)
        self.assertIn('sizes="192x192" href="/static/brand/postenza-icon-192.png"', page)
        favicon_markup = page.split("<head>", 1)[1].split("</head>", 1)[0]
        self.assertNotIn("postenza-logo", favicon_markup)
        self.assertIn('alt="Postenza"', page)
        self.assertNotIn("Sociora", page)
        self.assertNotIn("Social Media Bot", page)

    def test_automations_updates_direct_message_configuration_only_for_selected_account(self):
        """Direct-message copy and mode must remain account-scoped form settings."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add_all([
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a1.id,
                    enabled=True,
                    trigger_keywords=["hello"],
                    public_reply_enabled=False,
                    public_reply_text="",
                ),
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a2.id,
                    enabled=True,
                    trigger_keywords=["other"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    delivery_mode="direct_message",
                    private_message_text="Other account private message",
                    followup_message_text="Other account follow-up",
                ),
            ])

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        self.assertIn('id="delivery_mode"', page)
        self.assertIn('id="message_private_message_text"', page)
        self.assertIn('id="message_followup_message_text"', page)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)
        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "trigger_keywords": "hello",
                "public_reply_text": "",
                "delivery_mode": "direct_message",
                "private_message_text": "Configured private message",
                "followup_enabled": "on",
                "followup_delay_minutes": "60",
                "followup_message_text": "Configured follow-up",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.Session() as session:
            primary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
            secondary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a2.id
            ))
        self.assertEqual(primary.delivery_mode, "direct_message")
        self.assertEqual(primary.private_message_text, "Configured private message")
        self.assertEqual(primary.followup_message_text, "Configured follow-up")
        self.assertEqual(secondary.private_message_text, "Other account private message")
        self.assertEqual(secondary.followup_message_text, "Other account follow-up")

    def test_automations_present_customer_modes_and_safe_application_defaults(self):
        """Normal customers get complete Message/Link defaults without internal Prompt UI."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["prompt"],
                public_reply_enabled=False,
                public_reply_text="",
            ))

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)

        for value, label, description in (
            ("direct_message", "Message", "Send a custom private message."),
            ("link", "Link", "Send a link of your choice."),
        ):
            self.assertIn(f'value="{value}"', page)
            self.assertIn(f">{label}</option>", page)
            self.assertIn(description, page)
        self.assertNotIn('value="prompt_request"', page)
        self.assertNotIn('id="prompt_private_message_text"', page)
        self.assertIn('id="link_destination_url"', page)
        self.assertIn(
            "Hey 👋 Thanks for your interest! I’ve got something for you.",
            page,
        )
        self.assertIn("Thanks for commenting 🙌 I’ve sent you a DM.", page)
        self.assertIn("Hey 👋 Thanks for your interest! Here’s the link you asked for.", page)
        self.assertIn("Thanks 🙌 I’ve sent the link to your DMs.", page)
        self.assertNotIn('id="link_label"', page)

    def test_customer_automation_hides_prompt_and_rejects_forged_prompt_save(self):
        """A normal tenant must not discover or activate the internal Prompt mode."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["existing"],
                public_reply_enabled=False,
                public_reply_text="Existing public reply",
                delivery_mode="prompt_request",
                prompt_opening_message_text="Existing internal opening",
            ))

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        self.assertIn('value="direct_message"', page)
        self.assertIn('value="link"', page)
        self.assertNotIn('value="prompt_request"', page)
        self.assertNotIn("Prompt", page)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)

        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "delivery_mode": "prompt_request",
                "trigger_keywords": "forged",
                "public_reply_text": "Forged public",
                "prompt_opening_message_text": "Forged opening",
                "followup_delay_minutes": "60",
            },
        )

        self.assertEqual(response.status_code, 400)
        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
        self.assertEqual(config.delivery_mode, "prompt_request")
        self.assertEqual(config.trigger_keywords, ["existing"])
        self.assertEqual(config.public_reply_text, "Existing public reply")
        self.assertEqual(config.prompt_opening_message_text, "Existing internal opening")

    def test_customer_workspace_does_not_expose_internal_prompt_wording(self):
        """Prompt-specific analytics, content, and automation labels stay internal."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                delivery_mode="prompt_request",
                trigger_keywords=["prompt"],
                prompt_opening_message_text="Internal only copy",
            ))

        for path in ("automations", "analytics", "content"):
            page = self.client.get(
                f"/app/{path}?account={selected}"
            ).get_data(as_text=True)
            self.assertNotIn("Prompt", page, path)
            self.assertNotIn("Internal only copy", page, path)

    def test_customer_content_omits_internal_prompt_detail_fields_and_links(self):
        """Normal tenants must not receive owner-facing Prompt detail access."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)

        response = self.client.get(f"/app/api/content?account={selected}")
        self.assertEqual(response.status_code, 200)
        content = response.get_json()["content"]
        self.assertTrue(content)
        for item in content:
            self.assertNotIn("prompt_url", item)
            self.assertNotIn("public_id", item)
            self.assertNotIn("prompt_count", item)

        page = self.client.get(f"/app/content?account={selected}").get_data(as_text=True)
        self.assertNotIn("View interaction details", page)
        self.assertNotIn("/p/o/", page)

    def test_internal_owner_content_retains_prompt_detail_fields_and_links(self):
        """The established internal owner keeps existing Prompt detail access."""

        with self.Session.begin() as session:
            legacy = session.get(InstagramAccount, self.legacy_account.id)
            legacy.user_id = self.owner_a.id
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)

        response = self.client.get(f"/app/api/content?account={selected}")
        self.assertEqual(response.status_code, 200)
        content = response.get_json()["content"]
        self.assertTrue(content)
        self.assertEqual(content[0]["prompt_url"], "/p/o/opaque-prompt-a")
        self.assertEqual(content[0]["public_id"], "opaque-prompt-a")
        self.assertEqual(content[0]["prompt_count"], "1")

        page = self.client.get(f"/app/content?account={selected}").get_data(as_text=True)
        self.assertIn('href="/p/o/opaque-prompt-a"', page)
        self.assertIn("View interaction details", page)

    def test_internal_prompt_capability_uses_legacy_account_ownership_boundary(self):
        """Only the established legacy-account owner may view and save Prompt settings."""

        with self.Session.begin() as session:
            legacy = session.get(InstagramAccount, self.legacy_account.id)
            legacy.user_id = self.owner_a.id
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["prompt"],
                public_reply_enabled=True,
                public_reply_text="Saved prompt public reply",
                delivery_mode="prompt_request",
                prompt_opening_message_text="Saved prompt opening",
            ))

        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        self.assertIn('value="prompt_request"', page)
        self.assertIn("Saved prompt opening", page)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)
        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "delivery_mode": "prompt_request",
                "prompt_trigger_keywords": "prompt, access",
                "prompt_public_reply_enabled": "on",
                "prompt_public_reply_text": "Internal prompt public reply",
                "prompt_private_message_text": "Internal prompt opening",
                "prompt_followup_enabled": "on",
                "prompt_followup_delay_minutes": "360",
                "prompt_followup_message_text": "",
                "smart_reply_tone": "friendly",
                "smart_reply_max_per_hour": "20",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
        self.assertEqual(config.delivery_mode, "prompt_request")
        self.assertEqual(config.trigger_keywords, ["prompt", "access"])
        self.assertEqual(config.public_reply_text, "Internal prompt public reply")
        self.assertEqual(config.prompt_opening_message_text, "Internal prompt opening")
        internal_analytics = self.client.get(
            f"/app/analytics?account={selected}"
        ).get_data(as_text=True)
        self.assertIn("Prompt delivery details", internal_analytics)

        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_b.id,
                delivery_mode="direct_message",
                trigger_keywords=["send"],
            ))
        self._login_as(self.owner_b)
        foreign_tenant_page = self.client.get(
            f"/app/automations?account={self._reference(self.owner_b, self.account_b)}"
        ).get_data(as_text=True)
        self.assertNotIn('value="prompt_request"', foreign_tenant_page)
        self.assertNotIn("Internal prompt public reply", foreign_tenant_page)

    def test_switching_message_and_link_restores_each_modes_saved_customer_copy(self):
        """Activating another mode must snapshot and later restore every mode-specific field."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["messagecustom"],
                public_reply_enabled=False,
                public_reply_text="Message public custom",
                delivery_mode="direct_message",
                private_message_text="Message opening custom",
                followup_enabled=False,
                followup_delay_minutes=75,
                followup_message_text="Message follow-up custom",
            ))
        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)

        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "delivery_mode": "link",
                "message_trigger_keywords": "messagecustom",
                "message_public_reply_text": "Message public custom",
                "message_private_message_text": "Message opening custom",
                "message_followup_delay_minutes": "75",
                "message_followup_message_text": "Message follow-up custom",
                "link_trigger_keywords": "linkcustom, send",
                "link_public_reply_enabled": "on",
                "link_public_reply_text": "Link public custom",
                "link_private_message_text": "Link opening custom",
                "link_destination_url": "https://example.test/custom",
                "link_followup_enabled": "on",
                "link_followup_delay_minutes": "45",
                "link_followup_message_text": "Link follow-up custom",
                "smart_reply_tone": "friendly",
                "smart_reply_max_per_hour": "20",
            },
        )
        self.assertEqual(response.status_code, 200)
        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
            self.assertEqual(config.delivery_mode, "link")
            self.assertEqual(config.private_message_text, "Link opening custom")
            self.assertEqual(config.destination_url, "https://example.test/custom")
            self.assertEqual(
                config.inactive_mode_settings["modes"]["direct_message"]["private_message_text"],
                "Message opening custom",
            )

        restored_page = self.client.get(
            f"/app/automations?account={selected}"
        ).get_data(as_text=True)
        self.assertIn("Message opening custom", restored_page)
        self.assertIn("Message follow-up custom", restored_page)
        self.assertIn("Link opening custom", restored_page)
        restored_csrf = re.search(
            r'name="csrf_token"\s+value="([^"]+)"', restored_page
        ).group(1)
        restored_response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": restored_csrf,
                "enabled": "on",
                "delivery_mode": "direct_message",
                "smart_reply_tone": "friendly",
                "smart_reply_max_per_hour": "20",
            },
        )
        self.assertEqual(restored_response.status_code, 200)
        with self.Session() as session:
            restored_config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
        self.assertEqual(restored_config.delivery_mode, "direct_message")
        self.assertEqual(restored_config.trigger_keywords, ["messagecustom"])
        self.assertEqual(restored_config.public_reply_text, "Message public custom")
        self.assertEqual(restored_config.private_message_text, "Message opening custom")
        self.assertFalse(restored_config.followup_enabled)
        self.assertEqual(restored_config.followup_delay_minutes, 75)
        self.assertEqual(restored_config.followup_message_text, "Message follow-up custom")

    def test_link_automation_save_is_account_scoped_and_preserves_other_mode_copy(self):
        """Saving Link mode must validate/persist its URL without overwriting unrelated copy."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add_all([
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a1.id,
                    enabled=True,
                    trigger_keywords=["guide"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    prompt_opening_message_text="Keep prompt opening",
                ),
                InstagramAutomationConfig(
                    instagram_account_id=self.account_a2.id,
                    enabled=True,
                    trigger_keywords=["other"],
                    public_reply_enabled=False,
                    public_reply_text="",
                    delivery_mode="link",
                    private_message_text="Other link opening",
                    destination_url="https://other.example.test/resource",
                ),
            ])
        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)

        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "trigger_keywords": "guide",
                "public_reply_text": "",
                "delivery_mode": "link",
                "private_message_text": "Your download is ready.",
                "destination_url": " https://example.test/download?q=1 ",
                "followup_enabled": "on",
                "followup_delay_minutes": "60",
                "followup_message_text": "Did it help?",
            },
        )

        self.assertEqual(response.status_code, 200)
        with self.Session() as session:
            primary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
            secondary = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a2.id
            ))
        self.assertEqual(primary.delivery_mode, "link")
        self.assertEqual(primary.private_message_text, "Your download is ready.")
        self.assertEqual(primary.destination_url, "https://example.test/download?q=1")
        self.assertEqual(primary.prompt_opening_message_text, "Keep prompt opening")
        self.assertEqual(secondary.private_message_text, "Other link opening")
        self.assertEqual(secondary.destination_url, "https://other.example.test/resource")

    def test_link_automation_rejects_unsafe_url_without_mutating_saved_configuration(self):
        """A rejected scheme must leave the account's prior automation state untouched."""

        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=self.account_a1.id,
                enabled=True,
                trigger_keywords=["guide"],
                public_reply_enabled=False,
                public_reply_text="",
                delivery_mode="direct_message",
                private_message_text="Existing message",
            ))
        page = self.client.get(f"/app/automations?account={selected}").get_data(as_text=True)
        csrf_token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page).group(1)

        response = self.client.post(
            f"/app/automations?account={selected}",
            data={
                "csrf_token": csrf_token,
                "enabled": "on",
                "trigger_keywords": "guide",
                "public_reply_text": "",
                "delivery_mode": "link",
                "private_message_text": "Unsafe link",
                "destination_url": "javascript:alert(1)",
                "followup_enabled": "off",
                "followup_delay_minutes": "60",
                "followup_message_text": "",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        error_page = response.get_data(as_text=True)
        self.assertIn('class="alert alert-error" role="alert"', error_page)
        self.assertIn("Enter a valid http:// or https:// destination URL.", error_page)
        self.assertIn('value="link" selected', error_page)
        self.assertIn("Unsafe link", error_page)
        self.assertIn('value="javascript:alert(1)"', error_page)
        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id
            ))
        self.assertEqual(config.delivery_mode, "direct_message")
        self.assertEqual(config.private_message_text, "Existing message")
        self.assertIsNone(config.destination_url)

    def test_overview_automation_status_uses_selected_account_config(self):
        """A config change on one account cannot leak into another overview."""
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            session.add_all([
                InstagramAutomationConfig(instagram_account_id=self.account_a1.id, enabled=True),
                InstagramAutomationConfig(instagram_account_id=self.account_a2.id, enabled=False),
            ])

        enabled_page = self.client.get(
            f"/app?account={self._reference(self.owner_a, self.account_a1)}"
        ).get_data(as_text=True)
        disabled_page = self.client.get(
            f"/app?account={self._reference(self.owner_a, self.account_a2)}"
        ).get_data(as_text=True)

        self.assertIn("Automation is on", enabled_page)
        self.assertIn("Automation is off", disabled_page)

    def test_locale_selection_is_session_backed_and_sets_arabic_direction(self):
        """Changing the UI locale must not require or mutate a tenant record."""
        self._login_as(self.owner_a)
        response = self.client.post(
            "/app/locale",
            data={"locale": "ar", "csrf_token": "csrf-test-token"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('lang="ar"', page)
        self.assertIn('dir="rtl"', page)
        self.assertIn("نظرة عامة", page)

    def test_content_and_engagement_pages_use_general_product_copy(self):
        """Normal dashboard pages must not present Sociora as prompt delivery software."""
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)

        for path in ("content", "leads", "activity", "automations"):
            page = self.client.get(f"/app/{path}?account={selected}").get_data(as_text=True)
            self.assertNotIn("Prompt request", page)
            self.assertNotIn("prepared prompts", page.lower())

    def test_postenza_identity_is_preserved_for_each_supported_locale(self):
        """Locale selection must never translate or hide the product name."""
        self._login_as(self.owner_a)

        for locale in ("en", "fr", "ar"):
            with self.client.session_transaction() as browser_session:
                browser_session["_csrf_token"] = f"locale-{locale}-csrf-token-with-32-byte-minimum"
            response = self.client.post(
                "/app/locale",
                data={
                    "locale": locale,
                    "csrf_token": f"locale-{locale}-csrf-token-with-32-byte-minimum",
                },
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            page = self.client.get("/app").get_data(as_text=True)
            self.assertIn("Postenza", page)
            self.assertNotIn("Sociora", page)

    def test_current_tenant_pages_do_not_render_retired_sociora_branding(self):
        """A stale tenant template must not reveal the retired product name."""
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a1)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(instagram_account_id=self.account_a1.id))

        for path in ("automations", "content", "", "scheduler", "leads", "messages", "activity", "accounts"):
            with self.subTest(path=path or "overview"):
                route = "/app" if not path else f"/app/{path}"
                page = self.client.get(f"{route}?account={selected}").get_data(as_text=True)
                self.assertIn("Postenza", page)
                self.assertNotIn("Sociora", page)

    def test_activity_feed_uses_readable_time_without_exposing_raw_timestamp(self):
        """Activity presentation must not render the database ISO timestamp directly."""
        self._login_as(self.owner_a)
        page = self.client.get("/app/activity").get_data(as_text=True)

        self.assertIn("Times shown in Africa/Casablanca.", page)
        activity_markup = page.split('aria-label="Account activity"', 1)[1].split("</section>", 1)[0]
        self.assertIsNone(re.search(r"\d{4}-\d{2}-\d{2}T", activity_markup))
