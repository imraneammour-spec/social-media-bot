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
    InstagramCredential,
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

    def test_accounts_renders_safe_deployment_readiness_separately_from_active_account(self):
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
        self.assertIn("Deployment readiness: Ready", page)
        self.assertIn("Active", page)
        self.assertNotIn(key, page)

    def test_accounts_renders_safe_degraded_deployment_readiness(self):
        self._login_as(self.owner_a)
        with patch.dict("os.environ", {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true", "PUBLIC_BASE_URL": "http://unsafe.example"}, clear=False):
            page = self.client.get("/app/accounts").get_data(as_text=True)
        self.assertIn("Multiple OAuth deployment prerequisites are unavailable.", page)
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

    def test_accounts_show_reconnect_only_for_reconnectable_accounts(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a1.id)
            account.connection_status = "reconnect_required"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertEqual(page.count(">Reconnect Instagram</button>"), 1)
        self.assertIn('action="/connect/instagram"', page)

    def test_accounts_show_disconnect_only_for_disconnectable_accounts(self):
        with self.Session.begin() as session:
            account = session.get(InstagramAccount, self.account_a2.id)
            account.connection_status = "disconnected"

        self._login_as(self.owner_a)
        page = self.client.get("/app/accounts").get_data(as_text=True)

        self.assertEqual(page.count("Disconnect from this app"), 1)
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
            3,
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
        for path in ("/app", "/app/accounts", "/app/leads", "/app/activity", "/app/followups"):
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
        self.assertIn("Reply sent", {item["type"] for item in responses["activity"].get_json()["activity"]})
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
            "webhook",
        ):
            self.assertNotIn(forbidden, source)

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
        self.assertIn('aria-current="page">Home</a>', page)
        self.assertIn('href="/app/leads', page)
        self.assertIn('href="/app/automations', page)
        self.assertIn('href="/app/accounts', page)
        for label in ("Content", "Messages", "Scheduler", "Analytics"):
            self.assertIn(f">{label} <small>Coming soon</small>", page)
        self.assertEqual(page.count("Coming soon"), 4)
        self.assertNotIn('href="/dashboard', page)
        self.assertNotIn('href="/api/', page)

    def test_shell_active_navigation_remains_page_specific_and_opaque(self):
        self._login_as(self.owner_a)
        selected = self._reference(self.owner_a, self.account_a2)

        leads = self.client.get(f"/app/leads?account={selected}").get_data(as_text=True)
        accounts = self.client.get(f"/app/accounts?account={selected}").get_data(as_text=True)

        self.assertIn('aria-current="page">Leads</a>', leads)
        self.assertNotIn('aria-current="page">Home</a>', leads)
        self.assertIn('aria-current="page">Accounts</a>', accounts)
        self.assertNotIn(str(self.account_a2.id), leads)
        self.assertNotIn(self.account_a2.instagram_user_id, leads)

    def test_home_renders_existing_sanitized_status_and_summary_data(self):
        self._login_as(self.owner_a)

        page = self.client.get("/app").get_data(as_text=True)

        self.assertIn("Connection health", page)
        self.assertIn("Reconnect required", page)
        self.assertIn("Automation is not active", page)
        self.assertIn("Prompt posts", page)
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
        self.assertIn("No activity yet.", empty_page)

    def test_tenant_dashboard_stylesheet_is_served_without_legacy_routes(self):
        response = self.client.get("/static/tenant_dashboard.css")

        self.assertEqual(response.status_code, 200)
        stylesheet = response.get_data(as_text=True)
        self.assertIn(".app-shell", stylesheet)
        self.assertIn("@media", stylesheet)
        self.assertNotIn("/dashboard", stylesheet)
        self.assertNotIn("/api/", stylesheet)
        response.close()

