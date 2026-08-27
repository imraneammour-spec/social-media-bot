import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.models import Base, InstagramAccount, User


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **_kwargs):
        pass

    def start(self):
        return None

    def is_alive(self):
        return False


class WebhookTenantRoutingTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'webhook-routing.db'}"
        self.environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "integration-test-secret",
            "META_APP_SECRET": "routing-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "legacy-token-must-not-leak",
            "GEMINI_API_KEY": "",
        }
        module_name = "phase4b_webhook_tenant_routing"
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
        self._seed_accounts()

    def tearDown(self):
        self.session_factory.kw["bind"].dispose()
        sys.modules.pop("phase4b_webhook_tenant_routing", None)
        self.temp_dir.cleanup()

    def _seed_accounts(self):
        with self.session_factory.begin() as session:
            legacy_owner = User(email="legacy-route@example.test")
            oauth_owner = User(email="oauth-route@example.test")
            session.add_all([legacy_owner, oauth_owner])
            session.flush()
            session.add_all([
                InstagramAccount(
                    user_id=legacy_owner.id,
                    instagram_user_id=self.module.INSTAGRAM_USER_ID,
                    connection_status="environment_managed",
                ),
                InstagramAccount(
                    user_id=oauth_owner.id,
                    instagram_user_id="17841438617458902",
                    connection_status="connected",
                    provider="instagram",
                ),
                InstagramAccount(
                    user_id=oauth_owner.id,
                    instagram_user_id="17841438617458903",
                    connection_status="environment_managed",
                    provider="instagram",
                ),
            ])

    def _signed_headers(self, payload):
        body = json.dumps(payload).encode("utf-8")
        signature = hmac.new(
            self.environment["META_APP_SECRET"].encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return body, {"X-Hub-Signature-256": f"sha256={signature}"}

    def test_signed_legacy_entry_dispatches_but_connected_oauth_entry_is_non_operational(self):
        legacy_comment = {"id": "legacy-comment"}
        oauth_comment = {"id": "oauth-comment"}
        payload = {
            "object": "instagram",
            "entry": [
                {"id": self.module.INSTAGRAM_USER_ID, "changes": [{"field": "comments", "value": legacy_comment}]},
                {"id": "17841438617458902", "changes": [{"field": "comments", "value": oauth_comment}]},
            ],
        }
        with patch.object(self.module, "process_comment") as process_comment, patch.object(
            self.module, "process_message_event"
        ) as process_message, patch.object(self.module.threading, "Thread", _NoopThread):
            body, headers = self._signed_headers(payload)
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers
            )
            self.module.process_webhook_background(payload, "http://localhost")

        self.assertEqual(response.status_code, 200)
        process_comment.assert_called_once_with(legacy_comment, "http://localhost")
        process_message.assert_not_called()

    def test_legacy_messaging_requires_matching_recipient_when_recipient_is_supplied(self):
        matching = {"sender": {"id": "customer"}, "recipient": {"id": self.module.INSTAGRAM_USER_ID}}
        mismatch = {"sender": {"id": "customer"}, "recipient": {"id": "17841438617458902"}}
        malformed = {"sender": {"id": "customer"}, "recipient": {"id": "not-an-id"}}
        absent_recipient = {"sender": {"id": "customer"}}
        payload = {
            "entry": [{
                "id": self.module.INSTAGRAM_USER_ID,
                "messaging": [matching, mismatch, malformed, absent_recipient],
            }]
        }
        with patch.object(self.module, "process_message_event") as process_message, patch.object(
            self.module.threading, "Thread", _NoopThread
        ):
            body, headers = self._signed_headers(payload)
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers
            )
            self.module.process_webhook_background(payload, "http://localhost")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(process_message.call_args_list, [
            ((matching,), {}),
            ((absent_recipient,), {}),
        ])

    def test_oauth_entries_do_not_touch_legacy_handlers_json_graph_or_credential_boundary(self):
        payload = {
            "entry": [{
                "id": "17841438617458902",
                "changes": [{"field": "comments", "value": {"id": "oauth-comment"}}],
                "messaging": [{"sender": {"id": "customer"}, "recipient": {"id": "17841438617458902"}}],
            }]
        }
        with patch.object(self.module, "process_comment") as process_comment, patch.object(
            self.module, "process_message_event"
        ) as process_message, patch.object(self.module, "save_json_file") as save_json, patch.object(
            self.module.requests, "get"
        ) as graph_get, patch.object(self.module.requests, "post") as graph_post, patch(
            "database.account_runtime.credential_keyring_from_environment",
            side_effect=AssertionError("OAuth webhook routing must not load key material"),
        ), patch(
            "database.account_runtime.decrypt_access_token",
            side_effect=AssertionError("OAuth webhook routing must not decrypt credentials"),
        ):
            self.module.process_webhook_background(payload, "http://localhost")

        process_comment.assert_not_called()
        process_message.assert_not_called()
        save_json.assert_not_called()
        graph_get.assert_not_called()
        graph_post.assert_not_called()

    def test_unknown_malformed_and_nonlegacy_environment_accounts_skip_without_legacy_fallback(self):
        payload = {
            "entry": [
                {"id": "17841438617458999", "changes": [{"field": "comments", "value": {"id": "unknown"}}]},
                {"id": "not-an-id", "changes": [{"field": "comments", "value": {"id": "invalid"}}]},
                {"changes": [{"field": "comments", "value": {"id": "missing"}}]},
                {"id": "17841438617458903", "changes": [{"field": "comments", "value": {"id": "nonlegacy"}}]},
            ]
        }
        with patch.object(self.module, "process_comment") as process_comment:
            self.module.process_webhook_background(payload, "http://localhost")

        process_comment.assert_not_called()

    def test_disconnected_or_errored_oauth_accounts_skip_without_handler_activity(self):
        with self.session_factory.begin() as session:
            account = session.query(InstagramAccount).filter_by(
                instagram_user_id="17841438617458902"
            ).one()
            account.connection_status = "reconnect_required"
        payload = {"entry": [{"id": "17841438617458902", "changes": [{"field": "comments", "value": {}}]}]}
        with patch.object(self.module, "process_comment") as process_comment:
            self.module.process_webhook_background(payload, "http://localhost")

        process_comment.assert_not_called()

    def test_database_route_failure_skips_only_its_entry_and_later_legacy_entry_still_dispatches(self):
        payload = {
            "entry": [
                {"id": "17841438617458902", "changes": [{"field": "comments", "value": {"id": "bad"}}]},
                {"id": self.module.INSTAGRAM_USER_ID, "changes": [{"field": "comments", "value": {"id": "good"}}]},
            ]
        }
        original = self.module._resolve_webhook_entry_route

        def fail_first(external_id):
            if external_id == "17841438617458902":
                raise RuntimeError("database unavailable")
            return original(external_id)

        with patch.object(self.module, "_resolve_webhook_entry_route", side_effect=fail_first), patch.object(
            self.module, "process_comment"
        ) as process_comment:
            self.module.process_webhook_background(payload, "http://localhost")

        process_comment.assert_called_once_with({"id": "good"}, "http://localhost")

    def test_legacy_processing_failure_does_not_block_a_later_legacy_entry(self):
        payload = {
            "entry": [
                {"id": self.module.INSTAGRAM_USER_ID, "changes": [{"field": "comments", "value": {"id": "bad"}}]},
                {"id": self.module.INSTAGRAM_USER_ID, "changes": [{"field": "comments", "value": {"id": "good"}}]},
            ]
        }

        def fail_once(value, _public_base_url):
            if value["id"] == "bad":
                raise RuntimeError("unexpected handler failure")

        with patch.object(self.module, "process_comment", side_effect=fail_once) as process_comment:
            self.module.process_webhook_background(payload, "http://localhost")

        self.assertEqual(process_comment.call_count, 2)
        self.assertEqual(process_comment.call_args_list[-1].args[0]["id"], "good")

    def test_two_account_entries_keep_routing_state_isolated(self):
        first = {"id": "legacy-first"}
        second = {"id": "oauth-second"}
        payload = {
            "entry": [
                {"id": self.module.INSTAGRAM_USER_ID, "changes": [{"field": "comments", "value": first}]},
                {"id": "17841438617458902", "changes": [{"field": "comments", "value": second}]},
            ]
        }
        with patch.object(self.module, "process_comment") as process_comment:
            self.module.process_webhook_background(payload, "http://localhost")

        process_comment.assert_called_once_with(first, "http://localhost")

    def test_invalid_signature_prevents_background_thread_and_routing(self):
        with patch.object(self.module.threading, "Thread") as worker:
            response = self.client.post(
                "/webhook", data=b'{"entry": []}', content_type="application/json",
                headers={"X-Hub-Signature-256": "sha256=invalid"},
            )

        self.assertEqual(response.status_code, 403)
        worker.assert_not_called()

    def test_routing_logs_are_generic_and_do_not_disclose_sensitive_payload_content(self):
        sensitive_message = "private-message-content"
        sensitive_token = self.environment["INSTAGRAM_ACCESS_TOKEN"]
        payload = {"entry": [{"id": "invalid", "messaging": [{"message": {"text": sensitive_message}}]}]}
        with patch("builtins.print") as log:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(str(argument) for call in log.call_args_list for argument in call.args)
        self.assertNotIn(sensitive_message, rendered)
        self.assertNotIn(sensitive_token, rendered)


if __name__ == "__main__":
    unittest.main()
