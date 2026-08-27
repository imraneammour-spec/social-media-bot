import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile

from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import (
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from database.models import Base, InstagramAccount, InstagramPromptRequest, User
from tenant_dashboard import (
    lead_action_reference_for_prompt_request,
    register_tenant_dashboard,
)


class TenantLeadManagementTest(unittest.TestCase):
    """Behavior contract for account-scoped OAuth lead metadata writes."""

    CSRF_TOKEN = "csrf-tenant-lead-test-token-with-sufficient-entropy"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'leads.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        templates = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        self.app = Flask(__name__, template_folder=str(templates))
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="tenant-lead-management-test-secret",
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
            self.owner_a = User(email="lead-owner-a@example.invalid", display_name="Owner A")
            self.owner_b = User(email="lead-owner-b@example.invalid", display_name="Owner B")
            session.add_all([self.owner_a, self.owner_b])
            session.flush()
            self.account_a1 = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000001",
                username="alpha_one",
                connection_status="connected",
                created_at=now - timedelta(minutes=2),
            )
            self.account_a2 = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000002",
                username="alpha_two",
                connection_status="connected",
                created_at=now - timedelta(minutes=1),
            )
            self.account_b = InstagramAccount(
                user_id=self.owner_b.id,
                instagram_user_id="20000000000000001",
                username="bravo_private",
                connection_status="connected",
            )
            session.add_all([self.account_a1, self.account_a2, self.account_b])
            session.flush()
            self.lead_a1 = InstagramPromptRequest(
                instagram_account_id=self.account_a1.id,
                media_id="media-a1-private",
                comment_id="comment-a1-private",
                recipient_id="recipient-shared-private",
                username="lead_alpha",
                page_url="https://example.invalid/p/o/alpha",
                created_at=now,
            )
            self.lead_a2 = InstagramPromptRequest(
                instagram_account_id=self.account_a2.id,
                media_id="media-a2-private",
                comment_id="comment-a2-private",
                recipient_id="recipient-shared-private",
                username="lead_alpha_two",
                page_url="https://example.invalid/p/o/alpha-two",
                created_at=now - timedelta(minutes=1),
            )
            self.lead_b = InstagramPromptRequest(
                instagram_account_id=self.account_b.id,
                media_id="media-b-private",
                comment_id="comment-b-private",
                recipient_id="recipient-shared-private",
                username="lead_bravo",
                page_url="https://example.invalid/p/o/bravo",
                created_at=now,
            )
            session.add_all([self.lead_a1, self.lead_a2, self.lead_b])

    def _login_as(self, user):
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = str(user.id)
            browser_session["_csrf_token"] = self.CSRF_TOKEN

    def _account_reference(self, user, account):
        return action_reference_for_account(
            user.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
        )

    def _lead_reference(self, user, account, lead, purpose, *, now=None):
        return lead_action_reference_for_prompt_request(
            user.id,
            account.id,
            lead.id,
            purpose,
            secret_key=self.app.secret_key,
            now=now,
        )

    def _post(self, endpoint, user, account, lead, purpose, **data):
        account_reference = self._account_reference(user, account)
        form = {
            "csrf_token": self.CSRF_TOKEN,
            "lead_action": self._lead_reference(user, account, lead, purpose),
            **data,
        }
        return self.client.post(f"{endpoint}?account={account_reference}", data=form)

    def _lead(self, lead_id):
        with self.Session() as session:
            return session.get(InstagramPromptRequest, lead_id)

    def test_owner_can_update_every_allowed_base_stage(self):
        self._login_as(self.owner_a)
        for status in ("new", "contacted", "qualified", "won", "lost"):
            response = self._post(
                "/app/leads/status", self.owner_a, self.account_a1, self.lead_a1,
                "lead-status", lead_status=status,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(self._lead(self.lead_a1.id).lead_status, status)

    def test_note_trims_clears_and_never_renders_as_html(self):
        self._login_as(self.owner_a)
        response = self._post(
            "/app/leads/note", self.owner_a, self.account_a1, self.lead_a1,
            "lead-note", internal_note="  <script>private note</script>  ",
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self._lead(self.lead_a1.id).internal_note, "<script>private note</script>")
        page = self.client.get("/app/leads").get_data(as_text=True)
        self.assertIn("&lt;script&gt;private note&lt;/script&gt;", page)
        self.assertNotIn("<script>private note</script>", page)

        response = self._post(
            "/app/leads/note", self.owner_a, self.account_a1, self.lead_a1,
            "lead-note", internal_note="   ",
        )
        self.assertEqual(response.status_code, 303)
        self.assertIsNone(self._lead(self.lead_a1.id).internal_note)

    def test_archive_and_unarchive_preserve_the_base_stage(self):
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            session.get(InstagramPromptRequest, self.lead_a1.id).lead_status = "qualified"

        for operation, expected_archived in (("archive", True), ("archive", True), ("unarchive", False), ("unarchive", False)):
            response = self._post(
                "/app/leads/archive", self.owner_a, self.account_a1, self.lead_a1,
                "lead-archive", operation=operation,
            )
            self.assertEqual(response.status_code, 303)
            lead = self._lead(self.lead_a1.id)
            self.assertEqual(lead.lead_status, "qualified")
            self.assertEqual(lead.lead_archived_at is not None, expected_archived)

    def test_invalid_writes_are_safe_and_do_not_mutate(self):
        self._login_as(self.owner_a)
        initial = self._lead(self.lead_a1.id)
        invalid_status = self._post(
            "/app/leads/status", self.owner_a, self.account_a1, self.lead_a1,
            "lead-status", lead_status="published",
        )
        too_long_note = self._post(
            "/app/leads/note", self.owner_a, self.account_a1, self.lead_a1,
            "lead-note", internal_note="x" * 2001,
        )
        missing_csrf = self.client.post(
            f"/app/leads/status?account={self._account_reference(self.owner_a, self.account_a1)}",
            data={"lead_action": self._lead_reference(self.owner_a, self.account_a1, self.lead_a1, "lead-status"), "lead_status": "won"},
        )
        self.assertEqual([invalid_status.status_code, too_long_note.status_code, missing_csrf.status_code], [400, 400, 400])
        current = self._lead(self.lead_a1.id)
        self.assertEqual(current.lead_status, initial.lead_status)
        self.assertIsNone(current.internal_note)
        self.assertIsNone(current.lead_archived_at)

    def test_foreign_malformed_cross_account_and_expired_references_are_indistinguishable(self):
        self._login_as(self.owner_a)
        account_reference = self._account_reference(self.owner_a, self.account_a1)
        invalid_references = [
            "not-a-reference",
            self._lead_reference(self.owner_b, self.account_b, self.lead_b, "lead-status"),
            self._lead_reference(self.owner_a, self.account_a2, self.lead_a2, "lead-status"),
            self._lead_reference(self.owner_a, self.account_a1, self.lead_a1, "lead-status", now=0),
            self._lead_reference(self.owner_a, self.account_a1, InstagramPromptRequest(id=uuid.uuid4()), "lead-status"),
        ]
        responses = [
            self.client.post(
                f"/app/leads/status?account={account_reference}",
                data={"csrf_token": self.CSRF_TOKEN, "lead_action": reference, "lead_status": "won"},
            )
            for reference in invalid_references
        ]
        self.assertEqual([response.status_code for response in responses], [404] * len(responses))
        self.assertEqual({response.get_json()["error"] for response in responses}, {"Lead is unavailable."})
        self.assertEqual(self._lead(self.lead_a1.id).lead_status, "new")
        self.assertEqual(self._lead(self.lead_b.id).lead_status, "new")

    def test_filters_and_rendered_forms_are_account_scoped_and_secret_free(self):
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            session.get(InstagramPromptRequest, self.lead_a1.id).lead_status = "qualified"
            session.get(InstagramPromptRequest, self.lead_a2.id).lead_archived_at = datetime.now(timezone.utc)

        active = self.client.get(
            f"/app/api/leads?account={self._account_reference(self.owner_a, self.account_a1)}&stage=qualified"
        ).get_json()
        archived = self.client.get(
            f"/app/api/leads?account={self._account_reference(self.owner_a, self.account_a2)}&view=archived"
        ).get_json()
        self.assertEqual([lead["username"] for lead in active["leads"]], ["lead_alpha"])
        self.assertEqual([lead["username"] for lead in archived["leads"]], ["lead_alpha_two"])
        page = self.client.get(
            f"/app/leads?account={self._account_reference(self.owner_a, self.account_a1)}"
        ).get_data(as_text=True)
        for forbidden in (
            str(self.account_a1.id), str(self.lead_a1.id), "10000000000000001",
            "recipient-shared-private", "media-a1-private", "comment-a1-private",
            "bravo_private", "lead_bravo",
        ):
            self.assertNotIn(forbidden, page)
        self.assertIn('name="lead_action"', page)
        self.assertIn('name="csrf_token"', page)


if __name__ == "__main__":
    unittest.main()
