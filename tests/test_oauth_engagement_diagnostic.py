"""Phase 5D — OAuth engagement diagnostic regression tests.

Verifies that the bare ``except Exception:`` in ``_process_oauth_comment``
and ``_process_oauth_message`` no longer swallows the real failing condition.
The diagnostic helper must surface the exception type and a credential-safe
message so operators can identify the exact blocker (e.g. capability gate,
credential resolution, provider rejection) without exposing tokens, secrets,
or raw provider payloads.
"""

import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    InstagramAccount,
    InstagramAutomationConfig,
    InstagramCredential,
    User,
)


ROOT = Path(__file__).resolve().parents[1]
WEBHOOK_PATH = ROOT / "webhook.py"


class OAuthEngagementDiagnosticTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.temp_dir.name) / 'oauth-diag.db'}"
        environment = {
            "DATABASE_URL": database_url,
            "FLASK_SECRET_KEY": "oauth-diag-test-secret",
            "META_APP_SECRET": "oauth-diag-signing-secret",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "GEMINI_API_KEY": "",
        }
        module_name = f"phase5d_oauth_diag_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, WEBHOOK_PATH)
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = self.module
        with patch.dict(os.environ, environment, clear=False), patch("threading.Thread"):
            spec.loader.exec_module(self.module)
        self.module_name = module_name
        self.Session = self.module.app.extensions["phase3a_auth"].session_factory
        Base.metadata.create_all(self.Session.kw["bind"])
        with self.Session.begin() as session:
            user = User(email="diag@example.test")
            session.add(user)
            session.flush()
            account = InstagramAccount(
                user_id=user.id,
                instagram_user_id="17841438617459901",
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
                scopes=[
                    "instagram_business_basic",
                    "instagram_business_manage_comments",
                    "instagram_business_manage_messages",
                ],
            ))
            session.add(InstagramAutomationConfig(
                instagram_account_id=account.id,
                enabled=True,
                trigger_keywords=["prompt"],
                public_reply_enabled=False,
                public_reply_text="",
                smart_reply_enabled=False,
            ))

    def tearDown(self):
        self.Session.kw["bind"].dispose()
        sys.modules.pop(self.module_name, None)
        self.temp_dir.cleanup()

    def _route(self):
        from database.account_runtime import InstagramAccountRoute
        return InstagramAccountRoute(self.account_id, "oauth")

    def test_comment_diagnostic_surfaces_capability_gate_failure(self):
        """The capability gate blocks at the entry point before _process_oauth_comment.

        When ``INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED`` is not ``true``, the
        capability gate in ``process_oauth_webhook_entry`` returns early
        (line 4195-4200).  This proves the env var feature flag is the shared
        runtime blocker: webhook routing succeeds (route is selected), but
        comment processing is never reached because the capability pre-check
        fails.
        """
        from database.account_runtime import InstagramAccountRoute

        comment = {
            "id": "comment-diag-capability",
            "text": "prompt",
            "from": {"id": "customer-1", "username": "user_one"},
            "media": {"id": "media-1"},
        }
        entry = {"changes": [{"field": "comments", "value": comment}]}
        route = InstagramAccountRoute(self.account_id, "oauth")

        # INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED is NOT set, so the capability
        # gate returns "ready_for_activation" and process_oauth_webhook_entry
        # returns early without calling _process_oauth_comment.
        with patch.object(self.module, "_process_oauth_comment") as comment_proc, \
             patch.object(self.module, "_process_oauth_message") as msg_proc:
            self.module.process_oauth_webhook_entry(
                entry, route, "17841438617459901", "https://example.test"
            )

        comment_proc.assert_not_called()
        msg_proc.assert_not_called()

    def test_comment_diagnostic_surfaces_provider_rejection(self):
        """The diagnostic log must surface the real exception type on failure."""

        comment = {
            "id": "comment-diag-provider",
            "text": "prompt",
            "from": {"id": "customer-2", "username": "user_two"},
            "media": {"id": "media-2"},
        }
        entry = {"changes": [{"field": "comments", "value": comment}]}
        route = self._route()

        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), \
             patch.object(self.module, "send_oauth_private_access_reply", return_value=None), \
             patch("builtins.print") as log:
            self.module.process_oauth_webhook_entry(
                entry, route, "17841438617459901", "https://example.test"
            )

        diagnostic_calls = [
            str(call) for call in log.call_args_list
            if "OAUTH ENGAGEMENT" in str(call) and "comment" in str(call)
        ]
        self.assertTrue(len(diagnostic_calls) > 0)
        joined = " ".join(diagnostic_calls)
        self.assertIn("reason=", joined)
        # Must surface the real exception type (RuntimeError or
        # OAuthGraphOperationError depending on which condition fails)
        self.assertTrue(
            "RuntimeError" in joined or "OAuthGraphOperationError" in joined,
            f"Expected exception type in: {joined}",
        )

    def test_message_diagnostic_surfaces_failure_reason(self):
        """The message processing diagnostic must surface the failure reason."""

        from database.oauth_engagement import save_oauth_pending_access

        with self.Session.begin() as session:
            save_oauth_pending_access(
                session,
                self.account_id,
                "customer-3",
                "https://example.test/p/o/safe",
            )

        event = {
            "sender": {"id": "customer-3"},
            "message": {
                "mid": "msg-diag-1",
                "text": "View prompts",
                "quick_reply": {"payload": "VIEW_AI_PROMPTS"},
            },
        }
        route = self._route()

        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), \
             patch.object(self.module, "send_oauth_prompt_access_button", return_value=False), \
             patch("builtins.print") as log:
            self.module.process_oauth_webhook_entry(
                {"messaging": [event]}, route, "17841438617459901", "https://example.test"
            )

        diagnostic_calls = [
            str(call) for call in log.call_args_list
            if "OAUTH ENGAGEMENT" in str(call) and "message" in str(call)
        ]
        self.assertTrue(len(diagnostic_calls) > 0)
        joined = " ".join(diagnostic_calls)
        self.assertIn("reason=", joined)

    def test_diagnostic_never_prints_raw_exception_text(self):
        """The diagnostic must never print raw exception text that might contain secrets."""

        comment = {
            "id": "comment-diag-safe",
            "text": "prompt",
            "from": {"id": "customer-4", "username": "user_four"},
            "media": {"id": "media-4"},
        }
        entry = {"changes": [{"field": "comments", "value": comment}]}
        route = self._route()

        # Force an exception with a fake secret in the message.
        # send_oauth_private_access_reply is patched to raise directly,
        # so the exception type is RuntimeError.
        with patch.dict(os.environ, {"INSTAGRAM_OAUTH_ENGAGEMENT_ENABLED": "true"}, clear=False), \
             patch.object(
                 self.module,
                 "send_oauth_private_access_reply",
                 side_effect=RuntimeError("EAAbadtoken123secret456"),
             ), \
             patch("builtins.print") as log:
            self.module.process_oauth_webhook_entry(
                entry, route, "17841438617459901", "https://example.test"
            )

        # The raw secret must never appear in any print output
        all_output = " ".join(str(call) for call in log.call_args_list)
        self.assertNotIn("EAAbadtoken123secret456", all_output)
        # But the diagnostic should still surface the exception type
        self.assertTrue(
            "RuntimeError" in all_output or "OAuthGraphOperationError" in all_output,
            f"Expected exception type in: {all_output}",
        )

    def test_diagnostic_helper_is_credential_safe(self):
        """The standalone helper must sanitize unknown exception messages."""
        result = self.module._log_oauth_engagement_failure(
            RuntimeError("unknown-secret-payload"),
            context="comment",
        )
        self.assertIsNone(result)  # helper returns None (print only)

        # Verify via patching print
        with patch("builtins.print") as log:
            self.module._log_oauth_engagement_failure(
                RuntimeError("another-secret-token"),
                context="message",
            )
        logged = str(log.call_args)
        self.assertIn("reason=RuntimeError:sanitized", logged)
        self.assertNotIn("another-secret-token", logged)


if __name__ == "__main__":
    unittest.main()
