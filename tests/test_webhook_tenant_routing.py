import hashlib
import hmac
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from database.account_runtime import InstagramAccountRoute
from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramOAuthDirectMessageInteraction,
    InstagramOAuthFollowupJob,
    InstagramOAuthWebhookWorkItem,
    InstagramPendingAccess,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
    InstagramEngagementEvent,
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

    def _oauth_comment_payload(self, *, comment_id="17900000000000001"):
        return {
            "entry": [{
                "id": "17841438617458902",
                "changes": [{"field": "comments", "value": {
                    "id": comment_id,
                    "text": "private comment body",
                    "from": {"id": "17900000000000002", "username": "lead"},
                    "media": {"id": "17900000000000003"},
                }}],
            }],
        }

    def test_valid_oauth_webhook_is_persisted_before_fast_acknowledgment(self):
        payload = self._oauth_comment_payload()
        body, headers = self._signed_headers(payload)
        with patch.object(self.module.threading, "Thread", _NoopThread), patch.object(
            self.module, "process_oauth_webhook_entry"
        ) as oauth_handler:
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="https://public.example",
            )

        self.assertEqual(response.status_code, 200)
        oauth_handler.assert_not_called()
        with self.session_factory() as session:
            work = session.scalar(select(InstagramOAuthWebhookWorkItem))
        self.assertIsNotNone(work)
        self.assertEqual(work.state, "pending")

    def test_duplicate_oauth_delivery_creates_one_logical_work_item(self):
        payload = self._oauth_comment_payload()
        body, headers = self._signed_headers(payload)
        with patch.object(self.module.threading, "Thread", _NoopThread):
            first = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="https://public.example",
            )
            second = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="https://public.example",
            )

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        with self.session_factory() as session:
            self.assertEqual(
                len(session.scalars(select(InstagramOAuthWebhookWorkItem)).all()), 1
            )

    def test_oauth_ingress_persistence_failure_returns_non_success_without_worker(self):
        payload = self._oauth_comment_payload()
        body, headers = self._signed_headers(payload)
        with patch.object(
            self.module, "persist_oauth_webhook_ingress", side_effect=RuntimeError("unavailable")
        ), patch.object(self.module.threading, "Thread") as worker:
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="https://public.example",
            )

        self.assertEqual(response.status_code, 503)
        worker.assert_not_called()

    def test_pending_oauth_work_is_processed_once_after_restart_simulation(self):
        payload = self._oauth_comment_payload()
        body, headers = self._signed_headers(payload)
        with patch.object(self.module.threading, "Thread", _NoopThread):
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="https://public.example",
            )
        self.assertEqual(response.status_code, 200)

        with patch.object(self.module, "process_oauth_webhook_entry") as oauth_handler:
            self.module.process_due_oauth_webhook_work()
            self.module.process_due_oauth_webhook_work()

        oauth_handler.assert_called_once()
        route = oauth_handler.call_args.args[1]
        with self.session_factory() as session:
            account = session.scalar(select(InstagramAccount).where(
                InstagramAccount.instagram_user_id == "17841438617458902"
            ))
            work = session.scalar(select(InstagramOAuthWebhookWorkItem))
        self.assertEqual(route.instagram_account_id, account.id)
        self.assertEqual(work.state, "completed")

    def test_webhook_prefers_configured_public_https_base_url(self):
        payload = {"entry": []}
        body, headers = self._signed_headers(payload)
        with patch.object(self.module, "PUBLIC_BASE_URL", "https://public.example/app/", create=True), patch.object(
            self.module.threading, "Thread"
        ) as worker:
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="http://internal.example",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(worker.call_args.kwargs["args"][1], "https://public.example/app")

    def test_webhook_normalizes_proxied_http_base_to_https_in_deployed_environment(self):
        payload = {"entry": []}
        body, headers = self._signed_headers(payload)
        with patch.object(self.module, "PUBLIC_BASE_URL", "", create=True), patch.object(
            self.module, "IS_PRODUCTION", True
        ), patch.object(self.module.threading, "Thread") as worker:
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="http://staging.example",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(worker.call_args.kwargs["args"][1], "https://staging.example")

    def test_webhook_keeps_local_http_base_url_for_local_development(self):
        payload = {"entry": []}
        body, headers = self._signed_headers(payload)
        with patch.object(self.module, "PUBLIC_BASE_URL", "", create=True), patch.object(
            self.module, "IS_PRODUCTION", False
        ), patch.object(self.module.threading, "Thread") as worker:
            response = self.client.post(
                "/webhook", data=body, content_type="application/json", headers=headers,
                base_url="http://localhost:5000",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(worker.call_args.kwargs["args"][1], "http://localhost:5000")

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
        delivery_mode="prompt_request",
        private_message_text="",
        followup_enabled=True,
        followup_delay_minutes=360,
        followup_message_text="",
        smart_reply_enabled=False,
        smart_reply_tone="friendly",
        smart_reply_max_per_hour=20,
    ):
        account = self._account(external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramAutomationConfig(
                instagram_account_id=account.id,
                enabled=enabled,
                trigger_keywords=(trigger_keywords or ["launch"]),
                public_reply_enabled=public_reply_enabled,
                public_reply_text=public_reply_text,
                delivery_mode=delivery_mode,
                private_message_text=private_message_text,
                followup_enabled=followup_enabled,
                followup_delay_minutes=followup_delay_minutes,
                followup_message_text=followup_message_text,
                smart_reply_enabled=smart_reply_enabled,
                smart_reply_tone=smart_reply_tone,
                smart_reply_max_per_hour=smart_reply_max_per_hour,
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

    def _process_inbound_message(self, external_id, message_id, sender_id="recipient-general"):
        self.module._process_oauth_message(
            {
                "sender": {"id": sender_id},
                "recipient": {"id": external_id},
                "timestamp": datetime.now(timezone.utc).timestamp() * 1000,
                "message": {"mid": message_id, "text": "Thanks"},
            },
            self._route(external_id),
            external_id,
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

    def test_enabled_smart_reply_uses_its_account_tone_for_a_normal_comment(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
            smart_reply_tone="professional",
            smart_reply_max_per_hour=7,
        )
        with patch.object(
            self.module, "generate_oauth_smart_reply", return_value="Happy to help."
        ) as generate, patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply, patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page:
            self._process(self.primary_external_id, "smart-normal-comment", "How does this work?")

        generate.assert_called_once_with("How does this work?", tone="professional")
        public_reply.assert_called_once()
        prompt_page.assert_not_called()

    def test_disabled_smart_reply_does_not_reply_to_a_normal_comment(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=False,
        )
        with patch.object(self.module, "generate_oauth_smart_reply") as generate, patch.object(
            self.module, "send_oauth_public_comment_reply"
        ) as public_reply:
            self._process(self.primary_external_id, "smart-disabled-comment", "How does this work?")

        generate.assert_not_called()
        public_reply.assert_not_called()

    def test_keyword_automation_never_generates_a_smart_reply(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        with patch.object(self.module, "generate_oauth_smart_reply") as generate, patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_reply:
            self._process(self.primary_external_id, "keyword-priority", "Need a prompt")

        generate.assert_not_called()
        private_reply.assert_called_once()

    def test_smart_reply_limit_is_enforced_per_account(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
            smart_reply_max_per_hour=1,
        )
        primary = self._account(self.primary_external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=primary.id,
                comment_id="prior-smart-reply",
                recipient_id="customer-1",
                username="customer",
                reply_text="Already sent",
            ))

        with patch.object(self.module, "generate_oauth_smart_reply") as generate, patch.object(
            self.module, "send_oauth_public_comment_reply"
        ) as public_reply:
            self._process(self.primary_external_id, "rate-limited-comment", "How does this work?")

        generate.assert_not_called()
        public_reply.assert_not_called()

    def test_smart_reply_rate_activity_from_another_account_does_not_block_delivery(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
            smart_reply_max_per_hour=1,
        )
        self._set_config(
            self.secondary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
            smart_reply_tone="concise",
            smart_reply_max_per_hour=1,
        )
        primary = self._account(self.primary_external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=primary.id,
                comment_id="primary-only-rate-record",
                recipient_id="customer-1",
                username="customer",
                reply_text="Already sent",
            ))

        with patch.object(
            self.module, "generate_oauth_smart_reply", return_value="Sure."
        ) as generate, patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply:
            self._process(self.secondary_external_id, "secondary-not-limited", "How does this work?")

        generate.assert_called_once_with("How does this work?", tone="concise")
        public_reply.assert_called_once()

    def test_short_and_emoji_only_comments_are_not_sent_to_the_generator(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        with patch.object(self.module, "generate_oauth_smart_reply") as generate:
            self._process(self.primary_external_id, "short-comment", "ok")
            self._process(self.primary_external_id, "emoji-comment", "🔥🔥")

        generate.assert_not_called()

    def test_self_comment_is_ignored_before_smart_reply_generation(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        comment = self._comment("self-comment", "How does this work?")
        comment["from"]["id"] = self.primary_external_id
        with patch.object(self.module, "generate_oauth_smart_reply") as generate:
            self.module._process_oauth_comment(
                comment,
                self._route(self.primary_external_id),
                self.primary_external_id,
                "https://example.test",
                self.session_factory,
            )

        generate.assert_not_called()

    def test_duplicate_normal_comment_sends_one_smart_reply(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        with patch.object(
            self.module, "generate_oauth_smart_reply", return_value="Sure."
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply:
            self._process(self.primary_external_id, "duplicate-smart-comment", "How does this work?")
            self._process(self.primary_external_id, "duplicate-smart-comment", "How does this work?")

        public_reply.assert_called_once()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramSmartReplyActivity).filter_by(
                instagram_account_id=account.id,
                comment_id="duplicate-smart-comment",
            ).count(), 1)

    def test_smart_reply_generation_failure_completes_without_outbound_delivery(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        with patch.object(
            self.module, "generate_oauth_smart_reply", return_value=""
        ), patch.object(self.module, "send_oauth_public_comment_reply") as public_reply:
            self._process(self.primary_external_id, "generation-failure", "How does this work?")

        public_reply.assert_not_called()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            event = session.scalar(select(InstagramEngagementEvent).where(
                InstagramEngagementEvent.instagram_account_id == account.id,
                InstagramEngagementEvent.provider_event_id == "generation-failure",
            ))
        self.assertEqual(event.state, "done")

    def test_smart_reply_delivery_failure_marks_the_claim_failed_without_retry(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["prompt"],
            smart_reply_enabled=True,
        )
        with patch.object(
            self.module, "generate_oauth_smart_reply", return_value="Sure."
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=False
        ) as public_reply:
            self._process(self.primary_external_id, "delivery-failure", "How does this work?")
            self._process(self.primary_external_id, "delivery-failure", "How does this work?")

        public_reply.assert_called_once()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            event = session.scalar(select(InstagramEngagementEvent).where(
                InstagramEngagementEvent.instagram_account_id == account.id,
                InstagramEngagementEvent.provider_event_id == "delivery-failure",
            ))
        self.assertEqual(event.state, "failed")

    def test_direct_keyword_persists_interaction_but_creates_no_followup_before_reply(self):
        """Creating a job at comment time would send a prohibited second DM."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_text="Configured public reply",
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=True,
            followup_delay_minutes=60,
            followup_message_text="Configured general follow-up",
        )
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as public_reply, patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ) as private_reply:
            self._process(self.primary_external_id, "general-keyword", "hello there")

        prompt_page.assert_not_called()
        self.assertEqual(public_reply.call_args.kwargs["message"], "Configured public reply")
        self.assertEqual(private_reply.call_args.kwargs["message"], "Configured private reply")
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramPromptRequest).filter_by(
                instagram_account_id=account.id, comment_id="general-keyword"
            ).count(), 0)
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="general-keyword"
            ).count(), 0)
            interaction = session.query(InstagramOAuthDirectMessageInteraction).filter_by(
                instagram_account_id=account.id, source_comment_id="general-keyword"
            ).one()
        self.assertEqual(interaction.recipient_id, "recipient-general")
        self.assertIsNone(interaction.recipient_reply_at)

    def test_direct_message_recipient_reply_consumes_interaction_and_schedules_one_job(self):
        """Removing interaction consumption would let arbitrary or duplicate DMs create follow-ups."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=True,
            followup_delay_minutes=5,
            followup_message_text="Configured general follow-up",
        )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "reply-trigger-comment", "hello")
        self._process_inbound_message(self.primary_external_id, "recipient-reply-message")

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            interaction = session.query(InstagramOAuthDirectMessageInteraction).filter_by(
                instagram_account_id=account.id, source_comment_id="reply-trigger-comment"
            ).one()
            followup = session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="recipient-reply-message"
            ).one()
        self.assertEqual(interaction.recipient_reply_message_id, "recipient-reply-message")
        self.assertIsNotNone(interaction.recipient_reply_at)
        self.assertEqual(followup.message_text, "Configured general follow-up")
        self.assertEqual(
            followup.provider_window_expires_at - followup.run_at,
            timedelta(hours=23, minutes=55),
        )

    def test_queued_reply_uses_provider_timestamp_and_rejects_nonqualifying_events(self):
        """Queue latency, old messages and receipts must not manufacture a new reply window."""
        from database.oauth_webhook_ingress import _work_payload

        self._set_config(
            self.primary_external_id, delivery_mode="direct_message",
            followup_delay_minutes=5, followup_message_text="Configured follow-up",
        )
        account = self._account(self.primary_external_id)
        replied_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        with self.session_factory.begin() as session:
            session.add(InstagramOAuthDirectMessageInteraction(
                instagram_account_id=account.id, source_comment_id="queued-comment",
                recipient_id="recipient-general",
                initial_private_reply_at=replied_at - timedelta(minutes=1),
            ))
        base = {
            "sender": {"id": "recipient-general"},
            "recipient": {"id": self.primary_external_id},
            "timestamp": replied_at.timestamp() * 1000,
            "message": {"mid": "queued-reply", "text": "Thanks"},
        }
        for timestamp in (None, "invalid", True, (replied_at - timedelta(minutes=2)).timestamp() * 1000,
                          (replied_at + timedelta(days=2)).timestamp() * 1000,
                          (replied_at - timedelta(hours=25)).timestamp() * 1000):
            with self.subTest(timestamp_kind=type(timestamp).__name__):
                event = dict(base, timestamp=timestamp, message=dict(base["message"], mid=str(timestamp)))
                normalized = _work_payload("message", event)[2]
                self.module._process_oauth_message(
                    normalized, self._route(self.primary_external_id),
                    self.primary_external_id, self.session_factory,
                )
                with self.session_factory() as session:
                    self.assertEqual(session.query(InstagramOAuthFollowupJob).count(), 0)
                    self.assertIsNone(session.query(InstagramOAuthDirectMessageInteraction).one().recipient_reply_at)
        for extra, message in (
            ({}, {"mid": "empty-message"}),
            ({"delivery": {"mids": ["receipt"]}}, {"mid": "delivery-message", "text": "Thanks"}),
            ({"read": {"watermark": 1}}, {"mid": "read-message", "text": "Thanks"}),
            ({}, {"mid": "deleted-message", "is_deleted": True, "text": "Thanks"}),
        ):
            normalized = _work_payload("message", dict(base, **extra, message=message))[2]
            self.module._process_oauth_message(
                normalized, self._route(self.primary_external_id),
                self.primary_external_id, self.session_factory,
            )
            with self.session_factory() as session:
                self.assertEqual(session.query(InstagramOAuthFollowupJob).count(), 0)
        normalized = _work_payload("message", base)[2]
        self.module._process_oauth_message(
            normalized, self._route(self.primary_external_id),
            self.primary_external_id, self.session_factory,
        )
        with self.session_factory() as session:
            job = session.query(InstagramOAuthFollowupJob).one()
            self.assertAlmostEqual(
                job.provider_window_expires_at.replace(tzinfo=timezone.utc).timestamp(),
                (replied_at + timedelta(hours=24)).timestamp(), places=3,
            )
            self.assertAlmostEqual(job.run_at.replace(tzinfo=timezone.utc).timestamp(),
                                   (replied_at + timedelta(minutes=5)).timestamp(), places=3)

    def test_direct_message_does_not_persist_interaction_when_initial_private_delivery_fails(self):
        """Falling back to commenter data after a failed private send would create false eligibility."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
        )
        with patch.object(self.module, "send_oauth_private_comment_reply", return_value=None):
            self._process(self.primary_external_id, "failed-private-comment", "hello")

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthDirectMessageInteraction).filter_by(
                instagram_account_id=account.id, source_comment_id="failed-private-comment"
            ).count(), 0)

    def test_duplicate_direct_message_reply_creates_no_duplicate_followup(self):
        """Replaying an inbound webhook must not create a second job."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_message_text="Configured general follow-up",
        )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "duplicate-reply-comment", "hello")
        self._process_inbound_message(self.primary_external_id, "duplicate-reply-message")
        self._process_inbound_message(self.primary_external_id, "duplicate-reply-message")

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="duplicate-reply-message"
            ).count(), 1)

    def test_direct_message_reply_preserves_active_job_dedupe(self):
        """A second eligible reply must not create concurrent jobs for one account/recipient."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_message_text="Configured general follow-up",
        )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "first-active-comment", "hello")
        self._process_inbound_message(self.primary_external_id, "first-active-reply")
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "second-active-comment", "hello")
        self._process_inbound_message(self.primary_external_id, "second-active-reply")

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            jobs = session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, recipient_id="recipient-general"
            ).all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_message_id, "first-active-reply")

    def test_unrelated_direct_message_does_not_schedule_followup(self):
        """Removing the waiting-interaction lookup would schedule follow-ups for arbitrary DMs."""
        self._set_config(
            self.primary_external_id,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_message_text="Configured general follow-up",
        )
        self._process_inbound_message(self.primary_external_id, "unrelated-reply", "unrelated-recipient")

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="unrelated-reply"
            ).count(), 0)

    def test_direct_message_reply_cannot_consume_another_accounts_interaction(self):
        """Dropping the account predicate would leak follow-up eligibility across tenants."""
        for external_id, keyword in (
            (self.primary_external_id, "primary"),
            (self.secondary_external_id, "secondary"),
        ):
            self._set_config(
                external_id,
                trigger_keywords=[keyword],
                public_reply_enabled=False,
                delivery_mode="direct_message",
                private_message_text="Configured private reply",
                followup_message_text="Configured general follow-up",
            )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="shared-recipient"
        ):
            self._process(self.primary_external_id, "primary-only-interaction", "primary")
        self._process_inbound_message(
            self.secondary_external_id, "foreign-account-reply", "shared-recipient"
        )

        with self.session_factory.begin() as session:
            other_owner = User(email="other-direct-tenant@example.test")
            session.add(other_owner)
            session.flush()
            secondary_account = session.get(InstagramAccount, self._account(self.secondary_external_id).id)
            secondary_account.user_id = other_owner.id
        self._process_inbound_message(
            self.secondary_external_id, "foreign-tenant-reply", "shared-recipient"
        )

        primary = self._account(self.primary_external_id)
        secondary = self._account(self.secondary_external_id)
        with self.session_factory() as session:
            interaction = session.query(InstagramOAuthDirectMessageInteraction).filter_by(
                instagram_account_id=primary.id, source_comment_id="primary-only-interaction"
            ).one()
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=secondary.id, source_message_id="foreign-account-reply"
            ).count(), 0)
        self.assertIsNone(interaction.recipient_reply_at)

    def test_direct_message_echo_delivery_and_read_events_do_not_schedule_followup(self):
        """Non-user messaging events must not consume direct-message eligibility."""
        self._set_config(
            self.primary_external_id,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_message_text="Configured general follow-up",
        )
        route = self._route(self.primary_external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramOAuthDirectMessageInteraction(
                instagram_account_id=route.instagram_account_id,
                source_comment_id="receipt-interaction", recipient_id="recipient-general",
                initial_private_reply_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            ))
        for event in (
            {"sender": {"id": "recipient-general"}, "message": {"mid": "echo", "is_echo": True}},
            {"sender": {"id": "recipient-general"}, "delivery": {"mids": ["receipt"]}},
            {"sender": {"id": "recipient-general"}, "read": {"watermark": 1}},
        ):
            self.module._process_oauth_message(
                dict(event, timestamp=datetime.now(timezone.utc).timestamp() * 1000,
                     recipient={"id": self.primary_external_id}),
                route, self.primary_external_id, self.session_factory
            )

        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id
            ).count(), 0)

    def test_direct_message_reply_with_disabled_or_empty_followup_does_not_schedule(self):
        """Configuration gates must prevent scheduling without consuming a message into a job."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=False,
            followup_message_text="Configured general follow-up",
        )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "disabled-followup-comment", "hello")
        self._process_inbound_message(self.primary_external_id, "disabled-followup-reply")

        account = self._account(self.primary_external_id)
        with self.session_factory.begin() as session:
            config = session.query(InstagramAutomationConfig).filter_by(
                instagram_account_id=account.id
            ).one()
            config.followup_enabled = True
            config.followup_message_text = ""
        self._process_inbound_message(self.primary_external_id, "empty-followup-reply")

        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id
            ).count(), 0)

    def test_direct_message_reply_outside_provider_window_does_not_schedule(self):
        """A delay longer than Meta's 24-hour reply window must not create work."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_delay_minutes=1441,
            followup_message_text="Configured general follow-up",
        )
        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "outside-window-comment", "hello")
        with patch("builtins.print") as log:
            self._process_inbound_message(self.primary_external_id, "outside-window-reply")

        log.assert_any_call(
            "OAUTH FOLLOW-UP: recipient reply skipped. reason=outside_provider_window"
        )
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="outside-window-reply"
            ).count(), 0)

    def test_direct_keyword_with_empty_private_and_followup_text_skips_both(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="",
            followup_enabled=True,
            followup_message_text="",
        )
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_private_comment_reply"
        ) as private_reply:
            self._process(self.primary_external_id, "empty-general-keyword", "hello")

        prompt_page.assert_not_called()
        private_reply.assert_not_called()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="empty-general-keyword"
            ).count(), 0)

    def test_direct_message_reply_preserves_recent_sent_cooldown(self):
        """A deduped general follow-up must be observable without disclosing message data."""
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=True,
            followup_message_text="Configured general follow-up",
        )
        account = self._account(self.primary_external_id)
        with self.session_factory.begin() as session:
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=account.id,
                source_message_id="recent-sent-job",
                recipient_id="recipient-general",
                message_text="Prior follow-up",
                state="sent",
                run_at=datetime.now(timezone.utc),
                sent_at=datetime.now(timezone.utc),
            ))

        with patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ):
            self._process(self.primary_external_id, "deduped-general-keyword", "hello")
        with patch("builtins.print") as log:
            self._process_inbound_message(self.primary_external_id, "deduped-recipient-reply")

        log.assert_any_call(
            "OAUTH FOLLOW-UP: recipient reply skipped. reason=cooldown"
        )
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="deduped-recipient-reply"
            ).count(), 0)

    def test_duplicate_direct_keyword_does_not_duplicate_delivery_or_interaction(self):
        self._set_config(
            self.primary_external_id,
            trigger_keywords=["hello"],
            public_reply_enabled=False,
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=True,
            followup_message_text="Configured general follow-up",
        )
        with patch.object(self.module, "_oauth_prompt_post_for_comment") as prompt_page, patch.object(
            self.module, "send_oauth_private_comment_reply", return_value="recipient-general"
        ) as private_reply:
            self._process(self.primary_external_id, "duplicate-general-keyword", "hello")
            self._process(self.primary_external_id, "duplicate-general-keyword", "hello")

        prompt_page.assert_not_called()
        private_reply.assert_called_once()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="duplicate-general-keyword"
            ).count(), 0)
            self.assertEqual(session.query(InstagramOAuthDirectMessageInteraction).filter_by(
                instagram_account_id=account.id, source_comment_id="duplicate-general-keyword"
            ).count(), 1)

    def test_disabled_direct_keyword_skips_public_private_followup_and_smart_reply(self):
        self._set_config(
            self.primary_external_id,
            enabled=False,
            trigger_keywords=["hello"],
            delivery_mode="direct_message",
            private_message_text="Configured private reply",
            followup_enabled=True,
            followup_message_text="Configured general follow-up",
            smart_reply_enabled=True,
        )
        with patch.object(self.module, "generate_oauth_smart_reply") as smart_reply, patch.object(
            self.module, "send_oauth_public_comment_reply"
        ) as public_reply, patch.object(
            self.module, "send_oauth_private_comment_reply"
        ) as private_reply:
            self._process(self.primary_external_id, "disabled-general-keyword", "hello")

        smart_reply.assert_not_called()
        public_reply.assert_not_called()
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
        ) as private_button:
            self._process(self.primary_external_id, "custom-keyword", "Please launch this")

        private_button.assert_called_once()
        self.assertEqual(
            private_button.call_args.kwargs["comment_id"],
            "custom-keyword",
        )
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            requests = session.query(InstagramPromptRequest).filter_by(
                instagram_account_id=account.id, comment_id="custom-keyword"
            ).all()
            followups = session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="custom-keyword"
            ).all()
            pending_access = session.query(InstagramPendingAccess).filter_by(
                instagram_account_id=account.id, recipient_id="recipient-1"
            ).all()
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(followups), 1)
        self.assertEqual(len(pending_access), 1)

    def test_prompt_extraction_failure_marks_only_the_legacy_prompt_flow_failed(self):
        self._set_config(self.primary_external_id, trigger_keywords=["prompt"])
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value=""
        ), patch.object(self.module, "send_oauth_private_access_reply") as private_reply:
            self._process(self.primary_external_id, "prompt-extraction-failure", "prompt")

        private_reply.assert_not_called()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            event = session.scalar(select(InstagramEngagementEvent).where(
                InstagramEngagementEvent.instagram_account_id == account.id,
                InstagramEngagementEvent.provider_event_id == "prompt-extraction-failure",
            ))
        self.assertEqual(event.state, "failed")

    def test_duplicate_trigger_comment_sends_one_direct_prompt_button_and_followup(self):
        self._set_config(self.primary_external_id, trigger_keywords=["ai"])
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_button:
            self._process(self.primary_external_id, "duplicate-comment", "AI please")
            self._process(self.primary_external_id, "duplicate-comment", "AI please")

        private_button.assert_called_once()
        account = self._account(self.primary_external_id)
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramPromptRequest).filter_by(
                instagram_account_id=account.id, comment_id="duplicate-comment"
            ).count(), 1)
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=account.id, source_message_id="duplicate-comment"
            ).count(), 1)

    def test_direct_prompt_delivery_and_followups_remain_account_scoped(self):
        self._set_config(self.primary_external_id, trigger_keywords=["alpha"])
        self._set_config(self.secondary_external_id, trigger_keywords=["bravo"])
        with patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply",
            side_effect=["recipient-primary", "recipient-secondary"],
        ) as private_button:
            self._process(self.primary_external_id, "primary-comment", "alpha")
            self._process(self.secondary_external_id, "secondary-comment", "bravo")

        primary = self._account(self.primary_external_id)
        secondary = self._account(self.secondary_external_id)
        self.assertEqual(
            [call.kwargs["instagram_account_id"] for call in private_button.call_args_list],
            [primary.id, secondary.id],
        )
        with self.session_factory() as session:
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=primary.id, source_message_id="primary-comment"
            ).count(), 1)
            self.assertEqual(session.query(InstagramOAuthFollowupJob).filter_by(
                instagram_account_id=secondary.id, source_message_id="secondary-comment"
            ).count(), 1)

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
        ) as private_button:
            self._process(self.primary_external_id, "private-only", "AI please")

        public_reply.assert_not_called()
        private_button.assert_called_once()

    def test_other_account_keywords_do_not_match_this_account(self):
        self._set_config(self.primary_external_id, trigger_keywords=["alpha"])
        self._set_config(self.secondary_external_id, trigger_keywords=["bravo"])
        with patch.object(self.module, "generate_oauth_smart_reply", return_value=""), patch.object(
            self.module, "_oauth_prompt_post_for_comment", return_value="prompt-page"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ), patch.object(
            self.module, "send_oauth_private_access_reply", return_value="recipient-1"
        ) as private_button:
            self._process(self.primary_external_id, "other-account-keyword", "bravo")
            self._process(self.secondary_external_id, "secondary-account-keyword", "bravo")
            self._process(self.primary_external_id, "own-account-keyword", "alpha")

        self.assertEqual(private_button.call_count, 2)

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
