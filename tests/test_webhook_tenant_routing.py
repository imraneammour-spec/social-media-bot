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

    def test_connected_oauth_entry_dispatches_only_to_the_oauth_handler(self):
        payload = {"entry": [{"id": "17841438617458902", "changes": [{"field": "comments", "value": {"id": "oauth"}}]}]}
        with patch.object(self.module, "process_oauth_webhook_entry") as oauth_handler, patch.object(
            self.module, "process_comment"
        ) as legacy_handler, patch.object(self.module, "_webhook_entry_identity_classification") as identity, patch.object(
            self.module, "_failed_route_comment_event"
        ) as comment_event, patch.object(self.module, "_webhook_comment_test_reel_match") as reel_match, patch(
            "builtins.print"
        ) as log:
            self.module.process_webhook_background(payload, "http://localhost")

        oauth_handler.assert_called_once()
        legacy_handler.assert_not_called()
        identity.assert_not_called()
        comment_event.assert_not_called()
        reel_match.assert_not_called()
        messages = [call.args[0] for call in log.call_args_list]
        self.assertEqual(messages.count("WEBHOOK ROUTING: oauth_route_selected=True"), 1)
        rendered = " ".join(messages)
        self.assertNotIn("17841438617458902", rendered)
        self.assertNotIn(self.environment["INSTAGRAM_ACCESS_TOKEN"], rendered)

    def test_legacy_route_does_not_log_oauth_route_confirmation(self):
        payload = {
            "entry": [{
                "id": self.module.INSTAGRAM_USER_ID,
                "changes": [{"field": "comments", "value": {"id": "legacy"}}],
            }]
        }
        with patch.object(self.module, "process_comment") as legacy_handler, patch(
            "builtins.print"
        ) as log:
            self.module.process_webhook_background(payload, "http://localhost")

        legacy_handler.assert_called_once()
        self.assertNotIn(
            "WEBHOOK ROUTING: oauth_route_selected=True",
            [call.args[0] for call in log.call_args_list],
        )

    def test_failed_route_keeps_diagnostics_without_oauth_route_confirmation(self):
        payload = {
            "entry": [{
                "id": "17841438617458999",
                "changes": [{"field": "comments", "value": {"id": "unknown"}}],
            }]
        }
        with patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="unknown"
        ), patch.object(
            self.module, "_failed_route_comment_event", return_value=("comments", None)
        ), patch("builtins.print") as log:
            self.module.process_webhook_background(payload, "http://localhost")

        messages = [call.args[0] for call in log.call_args_list]
        self.assertNotIn("WEBHOOK ROUTING: oauth_route_selected=True", messages)
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: identity_classification=unknown", messages)
        self.assertIn("WEBHOOK ROUTING: entry skipped (route unavailable).", messages)

    def test_signed_oauth_webhook_acknowledgment_remains_200(self):
        payload = {"entry": [{"id": "17841438617458902", "changes": []}]}
        body, headers = self._signed_headers(payload)

        with patch.object(self.module.threading, "Thread", _NoopThread):
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers
            )

        self.assertEqual(response.status_code, 200)

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

    def test_route_failure_diagnostic_reports_matching_identity_without_leaking_data(self):
        entry_id = "17841438617458902"
        sensitive_token = self.environment["INSTAGRAM_ACCESS_TOKEN"]
        sensitive_payload = "private-message-content"
        payload = {
            "entry": [{
                "id": entry_id,
                "changes": [{"field": "comments", "value": {"text": sensitive_payload}}],
            }]
        }
        with patch.object(
            self.module,
            "_resolve_webhook_entry_route",
            side_effect=RuntimeError("forced route failure"),
        ), patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="connected_oauth"
        ) as classify, patch.object(self.module, "process_comment") as legacy_handler, patch.object(
            self.module, "process_oauth_webhook_entry"
        ) as oauth_handler, patch("builtins.print") as log:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        self.assertIn(
            "WEBHOOK ROUTING DIAGNOSTIC: identity_classification=connected_oauth", rendered
        )
        self.assertIn("WEBHOOK ROUTING: entry skipped (route unavailable).", rendered)
        for secret_or_identifier in (entry_id, sensitive_payload, sensitive_token):
            self.assertNotIn(secret_or_identifier, rendered)
        classify.assert_called_once_with(entry_id)
        legacy_handler.assert_not_called()
        oauth_handler.assert_not_called()

    def test_route_failure_diagnostic_reports_false_for_unknown_identity(self):
        payload = {
            "entry": [{
                "id": "17841438617458999",
                "changes": [{"field": "comments", "value": {}}],
            }]
        }
        with patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="unknown"
        ) as classify, patch("builtins.print") as log, patch.object(
            self.module, "process_comment"
        ) as legacy_handler:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: identity_classification=unknown", rendered)
        self.assertIn("WEBHOOK ROUTING: entry skipped (route unavailable).", rendered)
        self.assertNotIn("17841438617458999", rendered)
        classify.assert_called_once_with("17841438617458999")
        legacy_handler.assert_not_called()

    def test_route_failure_logs_one_safe_identity_classification_without_changing_dispatch(self):
        entry_id = "17841438617458999"
        sensitive_token = self.environment["INSTAGRAM_ACCESS_TOKEN"]
        sensitive_payload = "private-message-content"
        sensitive_username = "private-provider-username"
        payload = {
            "entry": [{
                "id": entry_id,
                "changes": [{
                    "field": "comments",
                    "value": {
                        "text": sensitive_payload,
                        "from": {"username": sensitive_username},
                    },
                }],
            }]
        }
        with patch.object(
            self.module,
            "_resolve_webhook_entry_route",
            side_effect=RuntimeError("forced route failure"),
        ), patch.object(
            self.module,
            "_webhook_entry_identity_classification",
            return_value="unknown",
        ) as classify, patch("builtins.print") as log, patch.object(
            self.module, "process_comment"
        ) as legacy_handler, patch.object(self.module, "process_oauth_webhook_entry") as oauth_handler:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: identity_classification=unknown", rendered)
        self.assertNotIn("identity_match=", rendered)
        self.assertNotIn("webhook_matches_graph_profile=", rendered)
        for secret_or_identifier in (
            entry_id,
            sensitive_token,
            sensitive_payload,
            sensitive_username,
        ):
            self.assertNotIn(secret_or_identifier, rendered)
        classify.assert_called_once_with(entry_id)
        legacy_handler.assert_not_called()
        oauth_handler.assert_not_called()

    def test_route_failure_diagnostic_logs_other_known_without_disclosing_data(self):
        entry_id = "17841438617458999"
        sensitive_token = self.environment["INSTAGRAM_ACCESS_TOKEN"]
        payload = {
            "entry": [{
                "id": entry_id,
                "changes": [{"field": "comments", "value": {"text": "private-message-content"}}],
            }]
        }
        with patch.object(
            self.module, "_resolve_webhook_entry_route", side_effect=RuntimeError("forced route failure")
        ), patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="other_known"
        ) as classify, patch("builtins.print") as log, patch.object(
            self.module, "process_comment"
        ) as legacy_handler, patch.object(self.module, "process_oauth_webhook_entry") as oauth_handler:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        self.assertIn(
            "WEBHOOK ROUTING DIAGNOSTIC: identity_classification=other_known", rendered
        )
        self.assertIn("WEBHOOK ROUTING: entry skipped (route unavailable).", rendered)
        for secret_or_identifier in (entry_id, sensitive_token, "private-message-content"):
            self.assertNotIn(secret_or_identifier, rendered)
        classify.assert_called_once_with(entry_id)
        legacy_handler.assert_not_called()
        oauth_handler.assert_not_called()

    def test_route_failure_diagnostic_classifies_every_route_failure_once(self):
        payload = {"entry": [{"id": "17841438617458902", "changes": []}]}
        with patch.object(
            self.module, "_resolve_webhook_entry_route", side_effect=RuntimeError("forced route failure")
        ), patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="unknown"
        ) as classify:
            self.module.process_webhook_background(payload, "http://localhost")

        classify.assert_called_once_with("17841438617458902")

    def test_route_failure_diagnostic_reports_indeterminate_when_lookup_is_unavailable(self):
        auth_state = self.module.app.extensions["phase3a_auth"]
        session_factory = auth_state.session_factory
        auth_state.session_factory = None
        payload = {
            "entry": [{
                "id": "17841438617458902",
                "changes": [{"field": "comments", "value": {}}],
            }]
        }
        try:
            with patch("builtins.print") as log, patch.object(
                self.module, "process_comment"
            ) as legacy_handler:
                self.module.process_webhook_background(payload, "http://localhost")
        finally:
            auth_state.session_factory = session_factory

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        self.assertIn(
            "WEBHOOK ROUTING DIAGNOSTIC: identity_classification=indeterminate", rendered
        )
        self.assertIn("WEBHOOK ROUTING: entry skipped (route unavailable).", rendered)
        self.assertNotIn("17841438617458902", rendered)
        legacy_handler.assert_not_called()

    def test_failed_route_comment_event_parser_accepts_only_documented_comment_media(self):
        self.assertEqual(
            self.module._failed_route_comment_event({
                "changes": [{"field": "comments", "value": {"media": {"id": "17890000000000001"}}}],
            }),
            ("comments", "17890000000000001"),
        )
        self.assertEqual(
            self.module._failed_route_comment_event({"changes": [{"field": "messages", "value": {}}]}),
            ("non_comments", None),
        )
        self.assertEqual(
            self.module._failed_route_comment_event({"changes": [{"field": "comments", "value": {}}]}),
            ("comments", None),
        )
        self.assertEqual(self.module._failed_route_comment_event({"changes": "invalid"}), ("indeterminate", None))

    def test_failed_comment_route_logs_match_and_never_discloses_identifiers(self):
        entry_id = "17841438617458999"
        media_id = "17890000000000001"
        comment_id = "17900000000000001"
        sensitive_token = self.environment["INSTAGRAM_ACCESS_TOKEN"]
        sensitive_username = "private-provider-username"
        payload = {"entry": [{
            "id": entry_id,
            "changes": [{"field": "comments", "value": {
                "id": comment_id,
                "media": {"id": media_id},
                "from": {"username": sensitive_username},
            }}],
        }]}
        with patch.object(self.module, "_resolve_webhook_entry_route", side_effect=RuntimeError("route failed")), patch.object(
            self.module, "_webhook_entry_identity_classification", return_value="unknown"
        ) as identity, patch.object(
            self.module, "_webhook_comment_test_reel_match", return_value="True"
        ) as reel_match, patch("builtins.print") as log, patch.object(
            self.module, "process_comment"
        ) as legacy_handler, patch.object(self.module, "process_oauth_webhook_entry") as oauth_handler:
            self.module.process_webhook_background(payload, "http://localhost")

        rendered = " ".join(str(argument) for call in log.call_args_list for argument in call.args)
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: identity_classification=unknown", rendered)
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: event_type=comments", rendered)
        self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: test_reel_match=True", rendered)
        for sensitive_value in (entry_id, media_id, comment_id, sensitive_token, sensitive_username):
            self.assertNotIn(sensitive_value, rendered)
        identity.assert_called_once_with(entry_id)
        reel_match.assert_called_once_with(media_id)
        legacy_handler.assert_not_called()
        oauth_handler.assert_not_called()

    def test_failed_route_non_comment_or_missing_media_stays_indeterminate_without_reel_lookup(self):
        cases = [
            ({"changes": [{"field": "messages", "value": {}}]}, "non_comments"),
            ({"changes": [{"field": "comments", "value": {}}]}, "comments"),
            ({"changes": "invalid"}, "indeterminate"),
        ]
        for entry, expected_event_type in cases:
            with self.subTest(expected_event_type=expected_event_type), patch.object(
                self.module, "_resolve_webhook_entry_route", side_effect=RuntimeError("route failed")
            ), patch.object(
                self.module, "_webhook_entry_identity_classification", return_value="unknown"
            ), patch.object(self.module, "_webhook_comment_test_reel_match") as reel_match, patch(
                "builtins.print"
            ) as log:
                entry["id"] = "17841438617458999"
                self.module.process_webhook_background({"entry": [entry]}, "http://localhost")

            rendered = " ".join(str(argument) for call in log.call_args_list for argument in call.args)
            self.assertIn(f"WEBHOOK ROUTING DIAGNOSTIC: event_type={expected_event_type}", rendered)
            self.assertIn("WEBHOOK ROUTING DIAGNOSTIC: test_reel_match=indeterminate", rendered)
            reel_match.assert_not_called()

    def test_route_failure_diagnostic_does_not_change_webhook_acknowledgment(self):
        payload = {"entry": [{"id": "17841438617458999", "changes": []}]}
        with patch.object(self.module.threading, "Thread", _NoopThread):
            body, headers = self._signed_headers(payload)
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers
            )

        self.assertEqual(response.status_code, 200)

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
