import importlib.util
import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from database.models import Base, InstagramAccount, User


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


class WebhookAuthenticationIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'webhook-auth.db'}"
        self.environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "integration-test-secret",
            "VERIFY_TOKEN": "integration-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
        }
        self.module_name = "phase3b_webhook_integration"
        self.module = self._load_webhook_module(self.environment, self.module_name)
        self.session_factory = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.session_factory.kw["bind"])
        self.client = self.module.app.test_client()

    def _load_webhook_module(self, environment, module_name):
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        with patch.dict(os.environ, environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)
        return module

    def tearDown(self):
        self.session_factory.kw["bind"].dispose()
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _create_legacy_owner(self):
        with self.session_factory.begin() as db_session:
            owner = User(
                email="legacy-owner@example.test",
                display_name="Legacy Owner",
                password_hash=generate_password_hash("correct horse battery staple"),
            )
            db_session.add(owner)
            db_session.flush()
            db_session.add(
                InstagramAccount(
                    user_id=owner.id,
                    instagram_user_id=self.module.INSTAGRAM_USER_ID,
                    username="legacy_owner",
                )
            )
            return owner.id

    def test_real_app_keeps_webhook_public_and_blocks_legacy_workspace(self):
        self.assertEqual(self.client.post("/webhook", json={}).status_code, 200)
        self.assertEqual(self.client.get("/dashboard").status_code, 302)
        self.assertEqual(self.client.get("/api/leads").status_code, 401)
        self.assertEqual(self.client.post("/api/publish-draft").status_code, 410)

    def test_real_app_allows_only_database_linked_owner_to_view_dashboard(self):
        owner_id = self._create_legacy_owner()
        csrf_value = "dashboard-csrf-token-with-32-byte-minimum"
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = str(owner_id)
            browser_session["_csrf_token"] = csrf_value

        response = self.client.get("/dashboard")

        # The legacy /dashboard route is retired and redirects to /app.
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/app")
        followed = self.client.get("/dashboard", follow_redirects=True)
        self.assertEqual(followed.status_code, 200)
        self.assertIn(b"Legacy Owner", followed.data)
        self.assertIn(csrf_value.encode("utf-8"), followed.data)
        with self.session_factory() as db_session:
            self.assertEqual(len(db_session.scalars(select(User)).all()), 1)

        health = self.client.get("/api/v01-health")
        self.assertEqual(health.status_code, 200)
        signature_check = next(
            check for check in health.get_json()["checks"] if check["key"] == "webhook_signature"
        )
        self.assertEqual(signature_check["status"], "warn")
        self.assertIn("NOT active", signature_check["detail"])

    def test_real_app_rejects_bad_meta_signature_when_secret_is_configured(self):
        self.module.app.config["META_APP_SECRET"] = "webhook-signing-test-secret"
        body = b'{ "object": "instagram", "entry": [] }'

        with patch.object(self.module.threading, "Thread") as rejected_thread, patch(
            "builtins.print"
        ) as printed:
            rejected = self.client.post(
                "/webhook", data=body, content_type="application/json",
                headers={"X-Hub-Signature-256": "sha256=bad"},
            )
        signature = hmac.new(
            b"webhook-signing-test-secret", body, hashlib.sha256
        ).hexdigest()
        with patch.object(self.module.threading, "Thread") as accepted_thread:
            accepted = self.client.post(
                "/webhook", data=body, content_type="application/json",
                headers={"X-Hub-Signature-256": f"sha256={signature}"},
            )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 200)
        rejected_thread.assert_not_called()
        accepted_thread.assert_called_once()
        rendered_logs = " ".join(
            str(argument)
            for call in printed.call_args_list
            for argument in call.args
        )
        self.assertNotIn("webhook-signing-test-secret", rendered_logs)
        self.assertNotIn("sha256=bad", rendered_logs)
        self.assertNotIn(body.decode("utf-8"), rendered_logs)

    def test_production_webhook_fails_closed_without_meta_app_secret(self):
        production_environment = dict(self.environment)
        production_environment.update(
            {
                "APP_ENV": "production",
                "META_APP_SECRET": "",
                "ALLOW_UNSIGNED_WEBHOOKS": "1",
            }
        )
        module_name = "phase3b_webhook_production_missing_secret"
        module = self._load_webhook_module(production_environment, module_name)
        try:
            with patch.object(module.threading, "Thread") as thread:
                response = module.app.test_client().post(
                    "/webhook", data=b'{"object":"instagram"}', content_type="application/json"
                )

            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.get_data(as_text=True).lower())
            thread.assert_not_called()
        finally:
            module.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
            sys.modules.pop(module_name, None)
            sys.modules.pop("legacy_bot", None)

    def test_missing_signature_is_rejected_in_production_before_processing(self):
        production_environment = dict(self.environment)
        production_environment.update(
            {
                "APP_ENV": "production",
                "META_APP_SECRET": "webhook-signing-test-secret",
                "ALLOW_UNSIGNED_WEBHOOKS": "1",
            }
        )
        module_name = "phase3b_webhook_production_missing_signature"
        module = self._load_webhook_module(production_environment, module_name)
        try:
            with patch.object(module.threading, "Thread") as thread:
                response = module.app.test_client().post(
                    "/webhook", data=b'{"object":"instagram"}', content_type="application/json"
                )

            self.assertEqual(response.status_code, 403)
            self.assertNotIn("webhook-signing-test-secret", response.get_data(as_text=True))
            thread.assert_not_called()
        finally:
            module.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
            sys.modules.pop(module_name, None)
            sys.modules.pop("legacy_bot", None)

    def test_explicit_test_bypass_accepts_unsigned_json_only_outside_production(self):
        body = b'{ "object": "instagram", "entry": [] }'

        with patch.object(self.module.threading, "Thread") as thread:
            response = self.client.post("/webhook", data=body, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        thread.assert_called_once()

    def test_development_bypass_requires_its_explicit_environment_flag(self):
        development_environment = dict(self.environment)
        development_environment.update(
            {
                "APP_ENV": "development",
                "META_APP_SECRET": "",
                "ALLOW_UNSIGNED_WEBHOOKS": "0",
            }
        )
        module_name = "phase3b_webhook_development_without_bypass"
        module = self._load_webhook_module(development_environment, module_name)
        try:
            with patch.object(module.threading, "Thread") as thread:
                response = module.app.test_client().post(
                    "/webhook", data=b'{"object":"instagram"}', content_type="application/json"
                )

            self.assertEqual(response.status_code, 503)
            thread.assert_not_called()
        finally:
            module.app.extensions["phase3a_auth"].session_factory.kw["bind"].dispose()
            sys.modules.pop(module_name, None)
            sys.modules.pop("legacy_bot", None)


if __name__ == "__main__":
    unittest.main()
