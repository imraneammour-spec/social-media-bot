import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database.account_runtime import InstagramAccountRuntime
from database.models import Base, InstagramAccount, InstagramCredential, User


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "webhook.py"


class OAuthEngagementModelTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            first = User(email="engagement-a@example.test")
            second = User(email="engagement-b@example.test")
            session.add_all([first, second])
            session.flush()
            account_a = InstagramAccount(user_id=first.id, instagram_user_id="17841438617458001", connection_status="connected")
            account_b = InstagramAccount(user_id=second.id, instagram_user_id="17841438617458002", connection_status="connected")
            session.add_all([account_a, account_b])
            session.flush()
            self.account_a_id = account_a.id
            self.account_b_id = account_b.id

    def tearDown(self):
        self.engine.dispose()

    def test_oauth_engagement_models_scope_identical_provider_identifiers_by_account(self):
        from database.models import InstagramEngagementEvent, InstagramPendingAccess, InstagramPromptPost

        with self.Session.begin() as session:
            session.add_all([
                InstagramEngagementEvent(instagram_account_id=self.account_a_id, provider_event_id="same-comment", event_type="comment", state="processing"),
                InstagramEngagementEvent(instagram_account_id=self.account_b_id, provider_event_id="same-comment", event_type="comment", state="processing"),
                InstagramPromptPost(instagram_account_id=self.account_a_id, media_id="same-media", public_id="public-a", prompts=[]),
                InstagramPromptPost(instagram_account_id=self.account_b_id, media_id="same-media", public_id="public-b", prompts=[]),
                InstagramPendingAccess(instagram_account_id=self.account_a_id, recipient_id="same-recipient", page_url="/p/o/public-a"),
                InstagramPendingAccess(instagram_account_id=self.account_b_id, recipient_id="same-recipient", page_url="/p/o/public-b"),
            ])

        with self.assertRaises(IntegrityError):
            with self.Session.begin() as session:
                session.add(InstagramEngagementEvent(instagram_account_id=self.account_a_id, provider_event_id="same-comment", event_type="comment", state="processing"))

    def test_durable_claim_is_account_scoped_and_replay_safe(self):
        from database.oauth_engagement import claim_oauth_event

        with self.Session.begin() as session:
            self.assertTrue(claim_oauth_event(session, self.account_a_id, "comment-1", "comment"))
        with self.Session.begin() as session:
            self.assertFalse(claim_oauth_event(session, self.account_a_id, "comment-1", "comment"))
            self.assertTrue(claim_oauth_event(session, self.account_b_id, "comment-1", "comment"))

    def test_pending_access_and_rate_activity_are_tenant_scoped(self):
        from database.oauth_engagement import (
            get_oauth_pending_access,
            oauth_request_on_cooldown,
            oauth_smart_reply_allowed,
            record_oauth_request,
            record_oauth_smart_reply,
            save_oauth_pending_access,
        )
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            save_oauth_pending_access(session, self.account_a_id, "same-person", "https://example.test/p/o/a")
            save_oauth_pending_access(session, self.account_b_id, "same-person", "https://example.test/p/o/b")
            record_oauth_request(session, self.account_a_id, "same-media", "a-comment", "same-person", "a", "https://example.test/p/o/a")
            record_oauth_smart_reply(session, self.account_a_id, "a-comment", "same-person", "a", "reply")
        with self.Session() as session:
            self.assertEqual(get_oauth_pending_access(session, self.account_a_id, "same-person"), "https://example.test/p/o/a")
            self.assertEqual(get_oauth_pending_access(session, self.account_b_id, "same-person"), "https://example.test/p/o/b")
            self.assertTrue(oauth_request_on_cooldown(session, self.account_a_id, "same-media", "same-person", cutoff=now - timedelta(minutes=1)))
            self.assertFalse(oauth_request_on_cooldown(session, self.account_b_id, "same-media", "same-person", cutoff=now - timedelta(minutes=1)))
            self.assertFalse(oauth_smart_reply_allowed(session, self.account_a_id, cutoff=now - timedelta(minutes=1), limit=1))
            self.assertTrue(oauth_smart_reply_allowed(session, self.account_b_id, cutoff=now - timedelta(minutes=1), limit=1))

    def test_oauth_graph_operations_resolve_distinct_runtime_tokens_without_legacy_fallback(self):
        from database import oauth_graph

        response = Mock(ok=True)
        response.json.return_value = {"id": "reply"}
        first = InstagramAccountRuntime(self.account_a_id, "17841438617458001", "oauth", "token-a")
        second = InstagramAccountRuntime(self.account_b_id, "17841438617458002", "oauth", "token-b")
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", side_effect=[first, second]
        ), patch.object(oauth_graph.requests, "request", return_value=response) as graph_request:
            self.assertTrue(oauth_graph.send_oauth_public_comment_reply(lambda: self.Session(), instagram_account_id=self.account_a_id, instagram_user_id="17841438617458001", comment_id="a-comment", message="hello"))
            self.assertTrue(oauth_graph.send_oauth_public_comment_reply(lambda: self.Session(), instagram_account_id=self.account_b_id, instagram_user_id="17841438617458002", comment_id="b-comment", message="hello"))
        self.assertEqual(graph_request.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer token-a")
        self.assertEqual(graph_request.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer token-b")
        self.assertNotIn("legacy", " ".join(str(call) for call in graph_request.call_args_list).lower())

    def test_oauth_graph_failures_are_generic_and_never_disclose_runtime_token(self):
        from database import oauth_graph

        secret_token = "oauth-token-that-must-not-leak"
        runtime = InstagramAccountRuntime(self.account_a_id, "17841438617458001", "oauth", secret_token)
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", return_value=runtime
        ), patch.object(oauth_graph.requests, "request", side_effect=oauth_graph.requests.RequestException(secret_token)):
            with self.assertRaises(oauth_graph.OAuthGraphOperationError) as raised:
                oauth_graph.lookup_oauth_media(lambda: self.Session(), instagram_account_id=self.account_a_id, instagram_user_id="17841438617458001", media_id="media")
        self.assertNotIn(secret_token, str(raised.exception))

    def test_prompt_access_button_logs_sanitized_provider_rejection_details(self):
        from database import oauth_graph

        secret_token = "oauth-token-that-must-not-leak"
        secret_recipient = "recipient-id-must-not-leak"
        secret_url = "https://example.test/p/o/secret-page"
        response = Mock(ok=False, status_code=400)
        response.json.return_value = {
            "error": {
                "code": 100,
                "error_subcode": 2018278,
                "message": (
                    "Template attachment rejected for "
                    f"{secret_recipient} at {secret_url} with {secret_token}"
                ),
            }
        }
        runtime = InstagramAccountRuntime(
            self.account_a_id, "17841438617458001", "oauth", secret_token
        )
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", return_value=runtime
        ), patch.object(oauth_graph.requests, "request", return_value=response) as graph_request, patch(
            "builtins.print"
        ) as log:
            sent = oauth_graph.send_oauth_prompt_access_button(
                lambda: self.Session(),
                instagram_account_id=self.account_a_id,
                instagram_user_id="17841438617458001",
                recipient_id=secret_recipient,
                page_url=secret_url,
            )

        self.assertFalse(sent)
        self.assertEqual(graph_request.call_args.args[0], "POST")
        self.assertTrue(
            graph_request.call_args.args[1].endswith("/17841438617458001/messages")
        )
        log.assert_called_once_with(
            "OAUTH PROMPT BUTTON: delivery_failed "
            "status=400 code=100 subcode=2018278 message=template_rejected"
        )
        rendered = " ".join(
            str(argument) for call in log.call_args_list for argument in call.args
        )
        for sensitive_value in (
            secret_token,
            secret_recipient,
            secret_url,
            str(self.account_a_id),
            "17841438617458001",
        ):
            self.assertNotIn(sensitive_value, rendered)

    def test_prompt_access_button_logs_finite_fallback_when_error_body_is_invalid(self):
        from database import oauth_graph

        response = Mock(ok=False, status_code=500)
        response.json.side_effect = ValueError("response body must not leak")
        runtime = InstagramAccountRuntime(
            self.account_a_id, "17841438617458001", "oauth", "safe-test-token"
        )
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", return_value=runtime
        ), patch.object(oauth_graph.requests, "request", return_value=response), patch(
            "builtins.print"
        ) as log:
            sent = oauth_graph.send_oauth_prompt_access_button(
                lambda: self.Session(),
                instagram_account_id=self.account_a_id,
                instagram_user_id="17841438617458001",
                recipient_id="recipient",
                page_url="https://example.test/p/o/page",
            )

        self.assertFalse(sent)
        log.assert_called_once_with(
            "OAUTH PROMPT BUTTON: delivery_failed "
            "status=500 code=none subcode=none message=provider_rejected"
        )


class OAuthWebhookEngagementTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'oauth-engagement.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "oauth-engagement-test-secret",
            "META_APP_SECRET": "oauth-engagement-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "legacy-token-must-not-be-used",
            "GEMINI_API_KEY": "",
        }
        spec = importlib.util.spec_from_file_location("phase4c_oauth_webhook", WEBHOOK_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules["phase4c_oauth_webhook"] = self.module
        with patch.dict(os.environ, environment, clear=False), patch("threading.Thread"):
            spec.loader.exec_module(self.module)
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            user_a, user_b = User(email="webhook-a@example.test"), User(email="webhook-b@example.test")
            session.add_all([user_a, user_b]); session.flush()
            account_a = InstagramAccount(user_id=user_a.id, instagram_user_id="17841438617458101", connection_status="connected")
            account_b = InstagramAccount(user_id=user_b.id, instagram_user_id="17841438617458102", connection_status="connected")
            session.add_all([account_a, account_b]); session.flush()
            self.account_a_id, self.account_b_id = account_a.id, account_b.id
            session.add_all([
                InstagramCredential(
                    instagram_account_id=account_a.id,
                    token_ciphertext="test-ciphertext-a",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                ),
                InstagramCredential(
                    instagram_account_id=account_b.id,
                    token_ciphertext="test-ciphertext-b",
                    encryption_key_id="KEY_2026_01",
                    token_status="valid",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                ),
            ])

    def tearDown(self):
        self.Session.kw["bind"].dispose()
        sys.modules.pop("phase4c_oauth_webhook", None)
        self.temp_dir.cleanup()

    def _route(self, account_id):
        from database.account_runtime import InstagramAccountRoute
        return InstagramAccountRoute(account_id, "oauth")

    def test_comment_replay_is_claimed_once_and_never_calls_legacy_json_or_graph_fallback(self):
        comment = {"id": "comment-a", "text": "hello", "from": {"id": "customer"}}
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), patch.object(
            self.module, "generate_oauth_smart_reply", return_value="Hello!"
        ), patch.object(
            self.module, "send_oauth_public_comment_reply", return_value=True
        ) as reply, patch.object(self.module, "save_json_file") as legacy_json, patch.object(
            self.module, "send_comment_reply_text"
        ) as legacy_graph:
            self.module.process_oauth_webhook_entry({"changes": [{"field": "comments", "value": comment}]}, self._route(self.account_a_id), "17841438617458101", "https://example.test")
            self.module.process_oauth_webhook_entry({"changes": [{"field": "comments", "value": comment}]}, self._route(self.account_a_id), "17841438617458101", "https://example.test")
        reply.assert_called_once()
        legacy_json.assert_not_called()
        legacy_graph.assert_not_called()

    def test_messages_require_mid_and_pending_access_is_scoped(self):
        from database.oauth_engagement import save_oauth_pending_access
        with self.Session.begin() as session:
            save_oauth_pending_access(session, self.account_a_id, "customer", "https://example.test/p/o/a")
            save_oauth_pending_access(session, self.account_b_id, "customer", "https://example.test/p/o/b")
        no_mid = {"sender": {"id": "customer"}, "message": {"text": "ACCESS"}}
        event = {"sender": {"id": "customer"}, "message": {"mid": "mid-a", "text": "ACCESS"}}
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), patch.object(
            self.module, "send_oauth_prompt_access_button", return_value=True
        ) as button:
            self.module.process_oauth_webhook_entry({"messaging": [no_mid, event, event]}, self._route(self.account_a_id), "17841438617458101", "https://example.test")
        button.assert_called_once()
        self.assertEqual(button.call_args.kwargs["page_url"], "https://example.test/p/o/a")

    def test_oauth_access_message_failure_keeps_generic_log_without_temporary_stage(self):
        from database.oauth_engagement import save_oauth_pending_access

        event = {"sender": {"id": "customer"}, "message": {"mid": "button-mid", "text": "ACCESS"}}
        with self.Session.begin() as session:
            save_oauth_pending_access(session, self.account_a_id, "customer", "http://internal.test/p/o/prompt")
        with patch.object(self.module, "send_oauth_prompt_access_button", return_value=False), patch(
            "builtins.print"
        ) as log:
            self.module._process_oauth_message(
                event, self._route(self.account_a_id), "17841438617458101", self.Session
            )

        log.assert_called_once_with(
            "OAUTH ENGAGEMENT: message processing unavailable."
        )

    def test_oauth_access_message_reaches_button_dispatch_for_https_pending_url(self):
        from database import oauth_graph
        from database.oauth_engagement import save_oauth_pending_access

        event = {"sender": {"id": "customer"}, "message": {"mid": "https-mid", "text": "ACCESS"}}
        with self.Session.begin() as session:
            save_oauth_pending_access(session, self.account_a_id, "customer", "https://example.test/p/o/prompt")
        with patch.object(oauth_graph, "_request_with_runtime", return_value={}) as graph_request:
            self.module._process_oauth_message(
                event, self._route(self.account_a_id), "17841438617458101", self.Session
            )

        self.assertEqual(
            graph_request.call_args.kwargs["json_body"]["message"]["attachment"]["payload"]["buttons"][0]["url"],
            "https://example.test/p/o/prompt",
        )

    def test_oauth_access_message_schedules_one_delayed_job_and_normal_message_cancels_only_its_account(self):
        from database.models import InstagramOAuthFollowupJob
        from database.oauth_engagement import save_oauth_pending_access
        from database.oauth_followups import schedule_oauth_followup

        with self.Session.begin() as session:
            save_oauth_pending_access(session, self.account_a_id, "customer", "https://example.test/p/o/a")
            schedule_oauth_followup(
                session,
                instagram_account_id=self.account_b_id,
                source_message_id="existing-b-job",
                recipient_id="customer",
                page_url="https://example.test/p/o/b",
            )
        access_event = {"sender": {"id": "customer"}, "message": {"mid": "access-mid-a", "text": "ACCESS"}}
        normal_event = {"sender": {"id": "customer"}, "message": {"mid": "normal-mid-a", "text": "thanks"}}
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), patch.object(
            self.module, "send_oauth_prompt_access_button", return_value=True
        ):
            self.module.process_oauth_webhook_entry({"messaging": [access_event, normal_event]}, self._route(self.account_a_id), "17841438617458101", "https://example.test")
        with self.Session() as session:
            account_a_jobs = list(session.query(InstagramOAuthFollowupJob).filter_by(instagram_account_id=self.account_a_id))
            account_b_job = session.query(InstagramOAuthFollowupJob).filter_by(instagram_account_id=self.account_b_id).one()
        self.assertEqual(len(account_a_jobs), 1)
        self.assertEqual(account_a_jobs[0].source_message_id, "access-mid-a")
        self.assertEqual(account_a_jobs[0].state, "cancelled")
        self.assertEqual(account_b_job.state, "pending")

    def test_unusable_oauth_account_performs_no_graph_action_or_legacy_fallback(self):
        comment = {"id": "comment-unusable", "text": "hello", "from": {"id": "customer"}}
        with patch.object(self.module, "generate_oauth_smart_reply", return_value="Hello!"), patch(
            "database.oauth_graph.requests.request"
        ) as graph_request, patch.object(self.module, "send_comment_reply_text") as legacy_reply, patch.object(
            self.module, "save_json_file"
        ) as legacy_json:
            self.module.process_oauth_webhook_entry({"changes": [{"field": "comments", "value": comment}]}, self._route(self.account_a_id), "17841438617458101", "https://example.test")
        graph_request.assert_not_called()
        legacy_reply.assert_not_called()
        legacy_json.assert_not_called()

    def test_oauth_prompt_page_uses_only_opaque_public_id_and_has_no_account_metadata(self):
        from database.oauth_engagement import oauth_prompt_post
        with self.Session.begin() as session:
            oauth_prompt_post(session, self.account_a_id, "media-a", [{"text": "safe prompt"}])
        with self.Session() as session:
            from database.oauth_engagement import get_oauth_prompt_post
            public_id = get_oauth_prompt_post(session, self.account_a_id, "media-a").public_id
        response = self.module.app.test_client().get(f"/p/o/{public_id}")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("safe prompt", body)
        self.assertNotIn(str(self.account_a_id), body)
        self.assertNotIn("17841438617458101", body)
        self.assertNotIn("media-a", body)


if __name__ == "__main__":
    unittest.main()
