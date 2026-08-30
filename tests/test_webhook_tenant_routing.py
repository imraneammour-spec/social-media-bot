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

from sqlalchemy import select

from database.account_runtime import InstagramAccountRoute
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramPendingAccess,
    User,
)


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
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
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
        ) as legacy_handler, patch(
            "builtins.print"
        ) as log:
            self.module.process_webhook_background(payload, "http://localhost")

        oauth_handler.assert_called_once()
        legacy_handler.assert_not_called()
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

    def test_failed_route_keeps_generic_routing_message_without_oauth_route_confirmation(self):
        payload = {
            "entry": [{
                "id": "17841438617458999",
                "changes": [{"field": "comments", "value": {"id": "unknown"}}],
            }]
        }
        with patch("builtins.print") as log:
            self.module.process_webhook_background(payload, "http://localhost")

        messages = [call.args[0] for call in log.call_args_list]
        self.assertNotIn("WEBHOOK ROUTING: oauth_route_selected=True", messages)
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

    def test_failed_route_does_not_make_a_diagnostic_graph_call(self):
        payload = {
            "entry": [{
                "id": "17841438617458999",
                "changes": [{"field": "comments", "value": {
                    "id": "17900000000000001",
                    "media": {"id": "17890000000000001"},
                }}],
            }]
        }
        with patch.object(
            self.module, "_resolve_webhook_entry_route", side_effect=RuntimeError("route failed")
        ), patch.object(self.module.requests, "get") as graph_get, patch(
            "builtins.print"
        ) as log:
            self.module.process_webhook_background(payload, "http://localhost")

        graph_get.assert_not_called()
        self.assertIn(
            "WEBHOOK ROUTING: entry skipped (route unavailable).",
            [call.args[0] for call in log.call_args_list],
        )
    def test_route_failure_does_not_change_webhook_acknowledgment(self):
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


class OAuthCommentAutomationConfigTest(unittest.TestCase):
    """Runtime coverage for account-scoped OAuth prompt-request settings."""

    primary_external_id = "17841438617458902"
    secondary_external_id = "17841438617458904"
    _seed_accounts = WebhookTenantRoutingTest._seed_accounts

    def setUp(self):
        WebhookTenantRoutingTest.setUp(self)
        with self.session_factory.begin() as session:
            owner = session.scalar(select(User).where(
                User.email == "oauth-route@example.test"
            ))
            session.add(InstagramAccount(
                user_id=owner.id,
                instagram_user_id=self.secondary_external_id,
                connection_status="connected",
                provider="instagram",
            ))

    def tearDown(self):
        WebhookTenantRoutingTest.tearDown(self)

    def _account(self, external_id):
        with self.session_factory() as session:
            return session.scalar(select(InstagramAccount).where(
                InstagramAccount.instagram_user_id == external_id
            ))

    def _route(self, external_id):
        account = self._account(external_id)
        return InstagramAccountRoute(account.id, "oauth")

    def _set_config(
        self,
        external_id,
        *,
        enabled=True,
        trigger_keywords=None,
        public_reply_enabled=True,
        public_reply_text="Configured acknowledgement",
    ):
        account = self._account(external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=account.id,
                enabled=enabled,
                trigger_keywords=(trigger_keywords or ["launch"]),
                public_reply_enabled=public_reply_enabled,
                public_reply_text=public_reply_text,
            ))

    @staticmethod
    def _comment(comment_id, text):
        return {
            "id": comment_id,
            "text": text,
            "from": {"id": "customer-1", "username": "customer"},
            "media": {"id": "media-1"},
        }

    def _process(self, external_id, comment_id, text):
        self.module._process_oauth_comment(
            self._comment(comment_id, text),
            self._route(external_id),
            external_id,
            "https://example.test",
            self.session_factory,
        )

    def test_disabled_automation_skips_new_prompt_request_delivery(self):
        self._set_config(
            self.primary_external_id,
            enabled=False,
            trigger_keywords=["ai"],
        )
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_private_access_reply"
        ) as private_reply:
            self._process(self.primary_external_id, "disabled-comment", "AI please")

        prompt_page.assert_not_called()
        private_reply.assert_not_called()

    def test_custom_account_keyword_starts_prompt_request_delivery(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["launch"],
        )
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_reply:
            self._process(self.primary_external_id, "custom-keyword", "Please launch this")

        private_reply.assert_called_once()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            pending = session.scalar(select(InstagramPendingAccess).where(
                InstagramPendingAccess.instagram_account_id == account.id,
                InstagramPendingAccess.recipient_id == "recipient-1",
            ))
        self.assertIsNotNone(pending)

    def test_prompt_request_uses_configured_public_reply_text(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["ai"],
            public_reply_text="Custom account acknowledgement",
        )
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply, patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ):
            self._process(self.primary_external_id, "custom-public-reply", "AI please")

        self.assertEqual(
            public_reply.call_args.kwargs["message"],
            "Custom account acknowledgement",
        )

    def test_public_reply_disabled_keeps_private_prompt_delivery(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["ai"],
            public_reply_enabled=False,
        )
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply, patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_reply:
            self._process(self.primary_external_id, "private-only", "AI please")

        public_reply.assert_not_called()
        private_reply.assert_called_once()

    def test_other_account_keywords_do_not_match_this_account(self):
        self._set_config(self.primary_external_id, trigger_keywords=["alpha"])
        self._set_config(self.secondary_external_id, trigger_keywords=["bravo"])
        with patch.object(self.module, "generate_oauth_smart_reply", return_value=""), patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_reply:
            self._process(self.primary_external_id, "other-account-keyword", "bravo")
            self._process(self.secondary_external_id, "secondary-account-keyword", "bravo")
            self._process(self.primary_external_id, "own-account-keyword", "alpha")

        self.assertEqual(private_reply.call_count, 2)

    def test_missing_runtime_config_skips_new_prompt_request_delivery(self):
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_private_access_reply"
        ) as private_reply:
            self._process(self.primary_external_id, "missing-config", "AI please")

        prompt_page.assert_not_called()
        private_reply.assert_not_called()

    def test_missing_runtime_config_emits_unavailable_prompt_decision(self):
        with patch.object(self.module, "generate_oauth_smart_reply", return_value=""), patch(
            "builtins.print"
        ) as log:
            self._process(self.primary_external_id, "missing-config-decision", "testauto")

        log.assert_any_call(
            "OAUTH AUTOMATION: prompt_request_decision=runtime_config_unavailable"
        )

    def test_nonmatching_runtime_config_emits_keyword_no_match_prompt_decision(self):
        self._set_config(self.primary_external_id, trigger_keywords=["launch"])
        with patch.object(self.module, "generate_oauth_smart_reply", return_value=""), patch(
            "builtins.print"
        ) as log:
            self._process(self.primary_external_id, "no-match-decision", "testauto")

        log.assert_any_call(
            "OAUTH AUTOMATION: prompt_request_decision=keyword_no_match"
        )

    def test_matching_runtime_config_emits_matched_prompt_decision(self):
        self._set_config(self.primary_external_id, trigger_keywords=["testauto"])
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ), patch("builtins.print") as log:
            self._process(self.primary_external_id, "matched-decision", "testauto")

        log.assert_any_call(
            "OAUTH AUTOMATION: prompt_request_decision=prompt_request_matched"
        )

    def test_prompt_decision_marker_never_contains_comment_or_config_values(self):
        secret_comment = "comment-text-must-not-appear"
        secret_keyword = "keyword-must-not-appear"
        secret_public_reply = "public-reply-must-not-appear"
        self._set_config(
            self.primary_external_id,
            trigger_keywords=[secret_keyword],
            public_reply_text=secret_public_reply,
        )
        with patch.object(self.module, "generate_oauth_smart_reply", return_value=""), patch(
            "builtins.print"
        ) as log:
            self._process(self.primary_external_id, "comment-id-must-not-appear", secret_comment)

        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        for sensitive_value in (
            "comment-id-must-not-appear",
            secret_comment,
            secret_keyword,
            secret_public_reply,
            "customer",
            self.primary_external_id,
            self.environment["INSTAGRAM_ACCESS_TOKEN"],
        ):
            self.assertNotIn(sensitive_value, rendered)

    def test_invalid_runtime_config_skips_new_prompt_request_delivery(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["two words"],
        )
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_private_access_reply"
        ) as private_reply:
            self._process(self.primary_external_id, "invalid-config", "AI please")

        prompt_page.assert_not_called()
        private_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
