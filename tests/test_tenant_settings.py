"""Tenant Settings page: profile, password, notifications, disconnect."""

import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash

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
    User,
)
from tenant_dashboard import register_tenant_dashboard


class TenantSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'tenant-settings.db'}"
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
            secret_key="tenant-settings-test-secret",
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
            self.owner = User(
                email="owner@example.invalid",
                display_name="Owner",
                password_hash=generate_password_hash("correct-horse-battery"),
            )
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

    def _csrf(self):
        page = self.client.get("/app/settings").get_data(as_text=True)
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        self.assertIsNotNone(match)
        return match.group(1)

    def _stored_user(self):
        with self.Session() as session:
            return session.scalar(select(User).where(User.id == self.owner.id))

    def test_settings_page_loads(self):
        self._login_as(self.owner)
        response = self.client.get("/app/settings")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Owner", page)
        self.assertIn("owner@example.invalid", page)
        self.assertIn("Current password", page)
        self.assertIn("Email me about new leads", page)
        self.assertIn("Weekly summary email", page)
        # Connected account can disconnect, so the section is shown.
        self.assertIn("Disconnect Instagram", page)
        self.assertIn('action="/connect/instagram/disconnect"', page)
        self.assertIn('name="account_action"', page)

    def test_sidebar_links_to_settings(self):
        self._login_as(self.owner)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertRegex(page, r'href="/app/settings\?account=[^"]*"')
        self.assertIn(">Settings</a>", page)

    def test_display_name_update_persists(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "profile",
                "display_name": "Renamed Owner",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Profile saved.", response.get_data(as_text=True))
        self.assertEqual(self._stored_user().display_name, "Renamed Owner")

    def test_password_change_with_correct_current_password(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "password",
                "current_password": "correct-horse-battery",
                "new_password": "brand-new-password-123",
                "confirm_password": "brand-new-password-123",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Password changed.", response.get_data(as_text=True))
        self.assertTrue(
            check_password_hash(
                self._stored_user().password_hash, "brand-new-password-123"
            )
        )

    def test_password_change_fails_with_wrong_current_password(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "password",
                "current_password": "not-the-password",
                "new_password": "brand-new-password-123",
                "confirm_password": "brand-new-password-123",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "Current password is incorrect.", response.get_data(as_text=True)
        )
        self.assertTrue(
            check_password_hash(
                self._stored_user().password_hash, "correct-horse-battery"
            )
        )

    def test_password_change_fails_when_confirmation_mismatches(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "password",
                "current_password": "correct-horse-battery",
                "new_password": "brand-new-password-123",
                "confirm_password": "different-password-123",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("New passwords do not match.", response.get_data(as_text=True))

    def test_notification_toggles_persist(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "notifications",
                "notify_weekly_summary": "on",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Notification preferences saved.", response.get_data(as_text=True)
        )
        prefs = self._stored_user().notification_preferences
        self.assertEqual(
            prefs, {"new_lead_email": False, "weekly_summary": True}
        )
        # A second save flips them back; the stored values round-trip.
        csrf = self._csrf()
        self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "notifications",
                "notify_new_lead_email": "on",
            },
        )
        prefs = self._stored_user().notification_preferences
        self.assertEqual(
            prefs, {"new_lead_email": True, "weekly_summary": False}
        )


if __name__ == "__main__":
    unittest.main()
