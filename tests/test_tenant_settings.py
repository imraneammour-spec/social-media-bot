"""Tenant Settings page: profile, password, notifications, disconnect."""

import os
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
    def _keyring_env(self):
        # One stable Fernet key per test case: save and test/remove must
        # decrypt with the same key.
        from cryptography.fernet import Fernet

        if not hasattr(self, "_keyring_key"):
            self._keyring_key = Fernet.generate_key().decode("ascii")
        return {
            "INSTAGRAM_CREDENTIAL_ACTIVE_KEY_ID": "K1",
            "INSTAGRAM_CREDENTIAL_KEY_K1": self._keyring_key,
        }

    def _stored_gemini_key(self):
        from database.models import UserGeminiKey

        with self.Session() as session:
            return session.scalar(
                select(UserGeminiKey).where(
                    UserGeminiKey.user_id == self.owner.id
                )
            )

    def _save_gemini_key(self, key, verdict="valid"):
        from unittest.mock import patch

        self._login_as(self.owner)
        csrf = self._csrf()
        with (
            patch.dict(os.environ, self._keyring_env(), clear=False),
            patch(
                "database.gemini_keys.validate_gemini_api_key",
                return_value=verdict,
            ),
        ):
            return self.client.post(
                "/app/settings",
                data={
                    "csrf_token": csrf,
                    "form": "gemini_key",
                    "gemini_action": "save",
                    "gemini_api_key": key,
                },
            )

    def test_gemini_key_section_shows_when_unconfigured(self):
        self._login_as(self.owner)
        response = self.client.get("/app/settings")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("Gemini API key", page)
        self.assertIn("No key stored", page)

    def test_gemini_key_save_valid_stores_encrypted_key(self):
        response = self._save_gemini_key("AIzaTestKey1234", verdict="valid")
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "Gemini API key saved and verified.",
            response.get_data(as_text=True),
        )
        row = self._stored_gemini_key()
        self.assertIsNotNone(row)
        self.assertEqual(row.key_hint, "1234")
        self.assertEqual(row.key_status, "active")
        self.assertNotIn("AIzaTestKey1234", row.key_ciphertext or "")
        self.assertIn("…1234", self.client.get("/app/settings").get_data(as_text=True))

    def test_gemini_key_save_invalid_is_rejected(self):
        response = self._save_gemini_key("AIzaBadKey", verdict="invalid")
        self.assertEqual(response.status_code, 400)
        self.assertIn("rejected by Google", response.get_data(as_text=True))
        self.assertIsNone(self._stored_gemini_key())

    def test_gemini_key_save_without_keyring_is_rejected(self):
        self._login_as(self.owner)
        csrf = self._csrf()
        response = self.client.post(
            "/app/settings",
            data={
                "csrf_token": csrf,
                "form": "gemini_key",
                "gemini_action": "save",
                "gemini_api_key": "AIzaTestKey1234",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not configured", response.get_data(as_text=True))
        self.assertIsNone(self._stored_gemini_key())

    def test_gemini_key_test_and_remove(self):
        from unittest.mock import patch

        self._save_gemini_key("AIzaTestKey1234", verdict="valid")
        self.assertIsNotNone(self._stored_gemini_key())

        self._login_as(self.owner)
        csrf = self._csrf()
        with (
            patch.dict(os.environ, self._keyring_env(), clear=False),
            patch(
                "database.gemini_keys.validate_gemini_api_key",
                return_value="invalid",
            ),
        ):
            response = self.client.post(
                "/app/settings",
                data={
                    "csrf_token": csrf,
                    "form": "gemini_key",
                    "gemini_action": "test",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("rejected by Google", response.get_data(as_text=True))
        self.assertEqual(self._stored_gemini_key().key_status, "invalid")

        csrf = self._csrf()
        with patch.dict(os.environ, self._keyring_env(), clear=False):
            response = self.client.post(
                "/app/settings",
                data={
                    "csrf_token": csrf,
                    "form": "gemini_key",
                    "gemini_action": "remove",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("removed", response.get_data(as_text=True))
        self.assertIsNone(self._stored_gemini_key())


if __name__ == "__main__":
    unittest.main()
