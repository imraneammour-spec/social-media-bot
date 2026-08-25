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
            "INSTAGRAM_ACCESS_TOKEN": "",
            "GEMINI_API_KEY": "",
        }
        module_name = "phase3a_webhook_integration"
        spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        with patch.dict(os.environ, self.environment, clear=False), patch(
            "threading.Thread", _NoopThread
        ):
            spec.loader.exec_module(self.module)
        self.session_factory = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.session_factory.kw["bind"])
        self.client = self.module.app.test_client()

    def tearDown(self):
        self.session_factory.kw["bind"].dispose()
        sys.modules.pop("phase3a_webhook_integration", None)
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

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Legacy Owner", response.data)
        self.assertIn(csrf_value.encode("utf-8"), response.data)
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
        body = b'{"object":"instagram"}'

        rejected = self.client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": "sha256=bad"},
        )
        signature = hmac.new(
            b"webhook-signing-test-secret", body, hashlib.sha256
        ).hexdigest()
        accepted = self.client.post(
            "/webhook", data=body, content_type="application/json",
            headers={"X-Hub-Signature-256": f"sha256={signature}"},
        )

        self.assertEqual(rejected.status_code, 403)
        self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
