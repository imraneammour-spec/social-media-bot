import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.account_runtime import InstagramAccountRuntime
from database.models import Base, InstagramAccount, InstagramCredential, User


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "webhook.py"


class OAuthGraphCapabilityGateTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            user = User(email="gate-graph@example.test")
            session.add(user)
            session.flush()
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617459501",
                provider="instagram",
                connection_status="connected",
            )
            session.add(account)
            session.flush()
            self.account_id = account.id
            session.add(InstagramCredential(
                instagram_account_id=account.id,
                token_ciphertext="non-empty-ciphertext",
                encryption_key_id="KEY_2026_01",
                token_status="valid",
                scopes=["instagram_business_basic"],
            ))

    def tearDown(self):
        self.engine.dispose()

    def test_basic_only_account_cannot_reach_comment_graph_dispatch(self):
        """Removing the gate would allow a basic-only account to send a comment reply."""
        from database import oauth_graph

        runtime = InstagramAccountRuntime(
            self.account_id, "17841438617459501", "oauth", "token-never-dispatched"
        )
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", return_value=runtime
        ) as resolver, patch.object(oauth_graph.requests, "request") as graph_request:
            sent = oauth_graph.send_oauth_public_comment_reply(
                lambda: self.Session(),
                instagram_account_id=self.account_id,
                instagram_user_id="17841438617459501",
                comment_id="comment-1",
                message="safe reply",
            )
            private_reply = oauth_graph.send_oauth_private_access_reply(
                lambda: self.Session(),
                instagram_account_id=self.account_id,
                instagram_user_id="17841438617459501",
                comment_id="comment-1",
            )

        self.assertFalse(sent)
        self.assertIsNone(private_reply)
        resolver.assert_not_called()
        graph_request.assert_not_called()

    def test_capability_assessment_infrastructure_failure_is_sanitized_and_fail_closed(self):
        """A database-session construction failure must not escape or activate engagement."""
        from database import oauth_graph

        def unavailable_session_factory():
            raise RuntimeError("database endpoint is unavailable")

        state = oauth_graph.oauth_engagement_capability_state(
            unavailable_session_factory,
            instagram_account_id=self.account_id,
            instagram_user_id="17841438617459501",
        )

        self.assertEqual(state, "temporarily_unavailable")


class OAuthWebhookCapabilityGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'oauth-gate.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "oauth-gate-test-secret",
            "META_APP_SECRET": "oauth-gate-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "legacy-token-must-not-be-read",
            "GEMINI_API_KEY": "",
        }
        module_name = f"phase5d_oauth_gate_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, WEBHOOK_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        with patch.dict(os.environ, environment, clear=False), patch("threading.Thread"):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(self.module)
        self.module_name = module_name
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            user = User(email="gate-webhook@example.test")
            session.add(user)
            session.flush()
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617459502",
                provider="instagram",
                connection_status="connected",
            )
            session.add(account)
            session.flush()
            self.account_id = account.id
            session.add(InstagramCredential(
                instagram_account_id=account.id,
                token_ciphertext="non-empty-ciphertext",
                encryption_key_id="KEY_2026_01",
                token_status="valid",
                scopes=["instagram_business_basic"],
            ))

    def tearDown(self):
        self.Session.kw["bind"].dispose()
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def test_default_disabled_entry_claims_nothing_and_never_reaches_graph_or_legacy(self):
        """The webhook must safely acknowledge a disabled OAuth account before processing."""
        from database.account_runtime import InstagramAccountRoute

        route = InstagramAccountRoute(self.account_id, "oauth")
        entry = {
            "changes": [{"field": "comments", "value": {
                "id": "comment-gate", "text": "hello", "from": {"id": "customer"},
            }}],
        }
        with patch.object(self.module, "claim_oauth_event") as claim, patch.object(
            self.module, "lookup_oauth_media"
        ) as lookup, patch.object(self.module, "send_oauth_public_comment_reply") as oauth_reply, patch.object(
            self.module, "send_comment_reply_text"
        ) as legacy_reply, patch.object(self.module, "save_json_file") as legacy_json:
            self.module.process_oauth_webhook_entry(
                entry, route, "17841438617459502", "https://example.test"
            )

        claim.assert_not_called()
        lookup.assert_not_called()
        oauth_reply.assert_not_called()
        legacy_reply.assert_not_called()
        legacy_json.assert_not_called()

    def test_due_non_active_followup_fails_terminally_and_is_not_retried(self):
        """A later reconnect or activation must not revive an already-blocked follow-up."""
        from database.models import InstagramOAuthFollowupJob
        from database.oauth_followups import schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_id,
                source_message_id="phase5d-capability-job",
                recipient_id="customer",
                page_url="https://example.test/p/o/safe",
                run_at=now - timedelta(seconds=1),
            )
            job_id = job.id

        with patch("database.oauth_graph.requests.post") as graph_post:
            self.module.process_due_oauth_followups(now=now)
            self.module.process_due_oauth_followups(now=now + timedelta(minutes=5))

        with self.Session() as session:
            job = session.get(InstagramOAuthFollowupJob, job_id)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.last_error_code, "capability_unavailable")
        self.assertEqual(job.attempt_count, 1)
        graph_post.assert_not_called()

    def test_followup_claim_failure_logs_safe_exception_type_only(self):
        """Claim diagnostics must identify a failure class without exposing its message."""
        with patch.object(
            self.module,
            "reap_expired_oauth_followup_leases",
            side_effect=RuntimeError("token=must-not-appear"),
        ), patch("builtins.print") as log:
            self.module.process_due_oauth_followups()

        log.assert_any_call(
            "OAUTH FOLLOW-UP: job claim unavailable. reason=RuntimeError:sanitized"
        )

    def test_expired_response_triggered_followup_fails_without_graph_delivery(self):
        """Removing the deadline guard would dispatch an outbound DM after the reply window."""
        from database.models import InstagramOAuthFollowupJob
        from database.oauth_followups import schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_id,
                source_message_id="expired-response-reply",
                recipient_id="expired-response-recipient",
                message_text="Configured follow-up",
                run_at=now - timedelta(minutes=1),
                provider_window_expires_at=now - timedelta(seconds=1),
            )
            job_id = job.id

        with patch.object(self.module, "send_oauth_delayed_followup") as send_followup:
            self.module.process_due_oauth_followups(now=now)

        with self.Session() as session:
            job = session.get(InstagramOAuthFollowupJob, job_id)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.last_error_code, "outside_provider_window")
        self.assertEqual(job.attempt_count, 1)
        send_followup.assert_not_called()

    def test_dispatch_boundary_expiry_is_terminal_and_clears_lease(self):
        """A deadline reached inside credential resolution must not become an ambiguous retry."""
        from database.models import InstagramOAuthFollowupJob
        from database.oauth_followups import schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session, instagram_account_id=self.account_id,
                source_message_id="boundary-expiry", recipient_id="boundary-recipient",
                message_text="Configured follow-up", run_at=now - timedelta(seconds=1),
                provider_window_expires_at=now + timedelta(seconds=1),
            )
            job_id = job.id
        with patch.object(self.module, "send_oauth_delayed_followup", return_value="outside_provider_window"):
            self.module.process_due_oauth_followups(now=now)
        with self.Session() as session:
            job = session.get(InstagramOAuthFollowupJob, job_id)
            self.assertEqual(job.state, "failed")
            self.assertEqual(job.last_error_code, "outside_provider_window")
            self.assertIsNone(job.lease_token)
            self.assertIsNone(job.lease_expires_at)
            self.assertEqual(job.attempt_count, 1)

    def test_in_window_response_triggered_followup_uses_existing_delivery_lifecycle(self):
        """A response-triggered job inside the window must still claim and finalize normally."""
        from database.models import InstagramOAuthFollowupJob
        from database.oauth_followups import schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_id,
                source_message_id="in-window-response-reply",
                recipient_id="in-window-response-recipient",
                message_text="Configured follow-up",
                run_at=now - timedelta(minutes=1),
                provider_window_expires_at=now + timedelta(hours=23),
            )
            job_id = job.id

        with patch.object(
            self.module, "send_oauth_delayed_followup", return_value="success"
        ) as send_followup:
            self.module.process_due_oauth_followups(now=now)

        with self.Session() as session:
            job = session.get(InstagramOAuthFollowupJob, job_id)
        self.assertEqual(job.state, "sent")
        self.assertIsNotNone(job.sent_at)
        self.assertEqual(job.attempt_count, 0)
        send_followup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
