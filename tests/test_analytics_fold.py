"""Analytics folded into Overview: /app/analytics redirects, section lives on /app."""

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
    User,
)
from tenant_dashboard import register_tenant_dashboard


class AnalyticsFoldTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'analytics-fold.db'}"
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
            secret_key="analytics-fold-test-secret",
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
            session.add(InstagramEngagementEvent(
                instagram_account_id=self.account.id,
                provider_event_id="provider-event-a-secret",
                event_type="message",
                state="done",
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

    def test_analytics_redirects_to_overview(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        response = self.client.get(f"/app/analytics?account={selected}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].startswith("/app?"),
            response.headers["Location"],
        )

    def test_overview_contains_analytics_section(self):
        self._login_as(self.owner)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('id="analytics"', page)
        self.assertIn("About these metrics", page)
        self.assertIn("Comment events", page)
        self.assertIn("Follow-up delivery", page)

    def test_sidebar_no_longer_links_to_analytics(self):
        self._login_as(self.owner)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertNotIn('href="/app/analytics', page)
        self.assertNotIn('data-nav="analytics"', page)

    def test_analytics_api_endpoint_still_serves(self):
        self._login_as(self.owner)
        selected = self._reference(self.owner, self.account)
        response = self.client.get(f"/app/api/analytics?account={selected}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("metrics", response.get_json())


if __name__ == "__main__":
    unittest.main()
