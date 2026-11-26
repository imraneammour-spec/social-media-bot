import importlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.account_runtime import InstagramAccountRuntime, RuntimeCredentialUnavailableError
from database.content import create_content_draft
from database.models import Base, InstagramAccount, InstagramContentItem, User


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "database" / "content_publishing.py"


class _ProviderResponse:
    def __init__(self, *, ok=True, status_code=None, payload=None):
        self.ok = ok
        self.status_code = status_code
        self._payload = {} if payload is None else payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class Phase7CScheduledPublishingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase7c.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        with self.Session.begin() as session:
            owner_a, owner_b = User(email="publish-a@example.test"), User(email="publish-b@example.test")
            session.add_all((owner_a, owner_b)); session.flush()
            self.account_a = InstagramAccount(user_id=owner_a.id, instagram_user_id="71000000000000001", username="publish_a", connection_status="connected")
            self.account_b = InstagramAccount(user_id=owner_b.id, instagram_user_id="72000000000000002", username="publish_b", connection_status="connected")
            session.add_all((self.account_a, self.account_b)); session.flush()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _api(self):
        self.assertTrue(SERVICE_PATH.exists(), "Phase 7C publishing service must exist")
        return importlib.import_module("database.content_publishing")

    def _scheduled(self, *, account=None, content_type="image", due=None):
        selected_account = account or self.account_a
        with self.Session.begin() as session:
            item = create_content_draft(
                session,
                instagram_account_id=selected_account.id,
                caption="Scheduled image",
                content_type=content_type,
                media_url="https://example.test/image.jpg",
            )
            item.status = "scheduled"
            item.scheduled_for = due if due is not None else self.now - timedelta(minutes=1)
            return item.id

    def _item(self, item_id):
        with self.Session() as session:
            return session.get(InstagramContentItem, item_id)

    def _runtime_for(self, account):
        return InstagramAccountRuntime(
            account.id, account.instagram_user_id, "oauth", "test-token-must-not-persist"
        )

    def test_future_scheduled_content_is_ignored_and_due_content_is_claimed_once(self):
        api = self._api()
        future = self._scheduled(due=self.now + timedelta(minutes=1))
        due = self._scheduled(due=self.now - timedelta(minutes=1))

        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=10)

        self.assertEqual([item.id for item in claimed], [due])
        self.assertEqual(self._item(future).status, "scheduled")
        self.assertEqual(self._item(due).status, "publishing")
        with self.Session.begin() as session:
            self.assertEqual(api.claim_due_scheduled_content(session, now=self.now, limit=10), [])

    def test_published_content_is_never_selected_again(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            item = session.get(InstagramContentItem, item_id)
            item.status = "published"
            item.published_at = self.now
            item.provider_media_id = "existing-media"

        with self.Session.begin() as session:
            self.assertEqual(api.claim_due_scheduled_content(session, now=self.now, limit=10), [])

    def test_successful_image_publish_persists_provider_id_and_is_not_repeated(self):
        api = self._api()
        item_id = self._scheduled()
        calls = []

        def resolver(session, *, instagram_account_id, instagram_user_id, now):
            self.assertEqual(instagram_account_id, self.account_a.id)
            self.assertEqual(instagram_user_id, self.account_a.instagram_user_id)
            return self._runtime_for(self.account_a)

        def publisher(runtime, item):
            calls.append((runtime.instagram_account_id, item.id))
            return "provider-media-a"

        first = api.execute_due_scheduled_content(
            self.Session, now=self.now, runtime_resolver=resolver, publisher=publisher
        )
        second = api.execute_due_scheduled_content(
            self.Session, now=self.now, runtime_resolver=resolver, publisher=publisher
        )
        item = self._item(item_id)

        self.assertEqual((first.claimed, first.published, first.failed), (1, 1, 0))
        self.assertEqual((second.claimed, second.published, second.failed), (0, 0, 0))
        self.assertEqual(calls, [(self.account_a.id, item_id)])
        self.assertEqual(item.status, "published")
        self.assertEqual(item.provider_media_id, "provider-media-a")
        self.assertEqual(item.published_at.replace(tzinfo=timezone.utc), self.now)
        self.assertIsNone(item.publish_error_code)
        self.assertFalse(item.provider_request_may_have_been_sent)

    def test_unsupported_content_type_fails_before_provider_or_credential_request(self):
        api = self._api()
        item_id = self._scheduled(content_type="carousel")
        invoked = []

        result = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: invoked.append("resolver"),
            publisher=lambda *args: invoked.append("publisher"),
        )
        item = self._item(item_id)

        self.assertEqual((result.claimed, result.failed), (1, 1))
        self.assertEqual(invoked, [])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "unsupported_content_type")
        self.assertFalse(item.provider_request_may_have_been_sent)

    def test_missing_tenant_connection_fails_with_safe_pre_provider_code(self):
        api = self._api()
        item_id = self._scheduled()

        def unavailable(*args, **kwargs):
            raise RuntimeCredentialUnavailableError("token-must-not-leak")

        result = api.execute_due_scheduled_content(
            self.Session, now=self.now, runtime_resolver=unavailable, publisher=lambda *args: "not-used"
        )
        item = self._item(item_id)

        self.assertEqual((result.claimed, result.failed), (1, 1))
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "instagram_connection_unavailable")
        self.assertFalse(item.provider_request_may_have_been_sent)
        self.assertNotIn("token-must-not-leak", str(item.publish_error_code))

    def test_tenant_identity_mismatch_does_not_publish_cross_tenant_content(self):
        api = self._api()
        item_id = self._scheduled(account=self.account_b)
        calls = []

        result = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            publisher=lambda *args: calls.append("publisher"),
        )
        item = self._item(item_id)

        self.assertEqual((result.claimed, result.failed), (1, 1))
        self.assertEqual(calls, [])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "tenant_identity_mismatch")
        self.assertFalse(item.provider_request_may_have_been_sent)

    def test_ambiguous_provider_outcome_is_terminal_and_never_resent(self):
        api = self._api()
        item_id = self._scheduled()
        calls = []

        def ambiguous(runtime, item):
            calls.append(item.id)
            raise api.InstagramPublishError("provider_request_ambiguous", request_may_have_been_sent=True)

        first = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            publisher=ambiguous,
        )
        second = api.execute_due_scheduled_content(
            self.Session,
            now=self.now,
            runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            publisher=ambiguous,
        )
        item = self._item(item_id)

        self.assertEqual((first.failed, second.claimed), (1, 0))
        self.assertEqual(calls, [item_id])
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "provider_request_ambiguous")
        self.assertTrue(item.provider_request_may_have_been_sent)

    def test_image_container_ready_immediately_is_published_once(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]

        with patch(
            "database.content_publishing.requests.post",
            side_effect=(
                _ProviderResponse(ok=True, status_code=200, payload={"id": "container-ready"}),
                _ProviderResponse(ok=True, status_code=200, payload={"id": "published-media"}),
            ),
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"status_code": "FINISHED"}),
        ) as get:
            provider_media_id = api.publish_instagram_image(self._runtime_for(self.account_a), claimed)

        self.assertEqual(provider_media_id, "published-media")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(post.call_count, 2)
        self.assertIn("media_publish", post.call_args_list[1].args[0])

    def test_image_container_is_polled_until_ready_before_single_publish(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]

        with patch(
            "database.content_publishing.requests.post",
            side_effect=(
                _ProviderResponse(ok=True, status_code=200, payload={"id": "container-polling"}),
                _ProviderResponse(ok=True, status_code=200, payload={"id": "published-media"}),
            ),
        ) as post, patch(
            "database.content_publishing.requests.get",
            side_effect=(
                _ProviderResponse(ok=True, status_code=200, payload={"status_code": "IN_PROGRESS"}),
                _ProviderResponse(ok=True, status_code=200, payload={"status_code": "IN_PROGRESS"}),
                _ProviderResponse(ok=True, status_code=200, payload={"status_code": "FINISHED"}),
            ),
        ) as get, patch("time.sleep") as sleep:
            provider_media_id = api.publish_instagram_image(self._runtime_for(self.account_a), claimed)

        self.assertEqual(provider_media_id, "published-media")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sum("media_publish" in call.args[0] for call in post.call_args_list), 1)

    def test_terminal_container_status_fails_lifecycle_without_media_publish(self):
        api = self._api()
        item_id = self._scheduled()
        with patch(
            "database.content_publishing.requests.post",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"id": "container-error"}),
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"status_code": "ERROR"}),
        ):
            result = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )

        item = self._item(item_id)
        self.assertEqual((result.claimed, result.published, result.failed), (1, 0, 1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "provider_container_failed")
        self.assertFalse(item.provider_request_may_have_been_sent)

    def test_never_ready_container_fails_without_media_publish_or_resend(self):
        api = self._api()
        item_id = self._scheduled()
        in_progress = _ProviderResponse(ok=True, status_code=200, payload={"status_code": "IN_PROGRESS"})
        with patch(
            "database.content_publishing.requests.post",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"id": "container-waiting"}),
        ) as post, patch(
            "database.content_publishing.requests.get", return_value=in_progress
        ) as get, patch("time.sleep"):
            first = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )
            second = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )

        item = self._item(item_id)
        self.assertEqual((first.claimed, first.published, first.failed), (1, 0, 1))
        self.assertEqual(second.claimed, 0)
        self.assertEqual(get.call_count, 6)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(item.publish_error_code, "provider_container_not_ready")
        self.assertFalse(item.provider_request_may_have_been_sent)

    def test_container_status_rejection_is_safe_and_never_calls_media_publish(self):
        api = self._api()
        item_id = self._scheduled()
        token = "provider-token-must-not-leak"
        with patch(
            "database.content_publishing.requests.post",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"id": "container-status-rejected"}),
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=_ProviderResponse(
                ok=False,
                status_code=429,
                payload={"error": {"type": "OAuthException", "code": 4, "error_subcode": 99, "access_token": token}},
            ),
        ), patch.object(api.LOGGER, "warning") as warning:
            result = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )

        item = self._item(item_id)
        self.assertEqual((result.claimed, result.published, result.failed), (1, 0, 1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(item.publish_error_code, "provider_rejected")
        self.assertFalse(item.provider_request_may_have_been_sent)
        warning.assert_any_call(
            "scheduled_content_publishing_failed "
            "publish_stage=container_status "
            "provider_http_status=429 "
            "provider_error_classification=provider_error_metadata "
            "provider_error_type=OAuthException "
            "provider_error_code=4 "
            "provider_error_subcode=99 "
            "provider_container_status=None"
        )
        self.assertNotIn(token, str(warning.call_args_list))

    def test_container_rejection_emits_safe_diagnostics_and_preserves_terminal_lifecycle(self):
        api = self._api()
        item_id = self._scheduled()
        authorization_token = "provider-token-must-not-leak"
        provider_secret = "provider-secret-must-not-leak"
        provider_response = _ProviderResponse(
            ok=False,
            status_code=400,
            payload={
                "error": {
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 460,
                    "message": provider_secret,
                    "access_token": authorization_token,
                }
            },
        )
        with patch("database.content_publishing.requests.post", return_value=provider_response) as post, patch.object(
            api.LOGGER, "warning"
        ) as warning:
            result = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )

        item = self._item(item_id)
        self.assertEqual((result.claimed, result.published, result.failed), (1, 0, 1))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.publish_error_code, "provider_rejected")
        self.assertFalse(item.provider_request_may_have_been_sent)
        warning.assert_any_call(
            "scheduled_content_publishing_failed "
            "publish_stage=container_create "
            "provider_http_status=400 "
            "provider_error_classification=provider_error_metadata "
            "provider_error_type=OAuthException "
            "provider_error_code=190 "
            "provider_error_subcode=460 "
            "provider_container_status=None"
        )
        for sensitive_value in (authorization_token, provider_secret, "test-token-must-not-persist"):
            self.assertNotIn(sensitive_value, str(warning.call_args_list))

    def test_final_publish_rejection_emits_media_publish_diagnostics(self):
        api = self._api()
        item_id = self._scheduled()
        with patch(
            "database.content_publishing.requests.post",
            side_effect=(
                _ProviderResponse(ok=True, status_code=200, payload={"id": "container-id"}),
                _ProviderResponse(
                    ok=False,
                    status_code=403,
                    payload={
                        "error": {
                            "type": "IGApiException",
                            "code": 10,
                            "error_subcode": 2018001,
                        }
                    },
                ),
            ),
        ) as post, patch(
            "database.content_publishing.requests.get",
            return_value=_ProviderResponse(ok=True, status_code=200, payload={"status_code": "FINISHED"}),
        ), patch.object(api.LOGGER, "warning") as warning:
            result = api.execute_due_scheduled_content(
                self.Session,
                now=self.now,
                runtime_resolver=lambda *args, **kwargs: self._runtime_for(self.account_a),
            )

        item = self._item(item_id)
        self.assertEqual((result.claimed, result.published, result.failed), (1, 0, 1))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(item.publish_error_code, "provider_rejected")
        self.assertFalse(item.provider_request_may_have_been_sent)
        warning.assert_any_call(
            "scheduled_content_publishing_failed "
            "publish_stage=media_publish "
            "provider_http_status=403 "
            "provider_error_classification=provider_error_metadata "
            "provider_error_type=IGApiException "
            "provider_error_code=10 "
            "provider_error_subcode=2018001 "
            "provider_container_status=None"
        )

    def test_malformed_provider_rejection_is_safely_classified_without_payload(self):
        api = self._api()
        item_id = self._scheduled()
        with self.Session.begin() as session:
            claimed = api.claim_due_scheduled_content(session, now=self.now, limit=1)[0]
        provider_secret = "provider-body-must-not-leak"
        with patch(
            "database.content_publishing.requests.post",
            return_value=_ProviderResponse(
                ok=False, status_code=502, payload=ValueError(provider_secret)
            ),
        ):
            with self.assertRaises(api.InstagramPublishError) as error:
                api.publish_instagram_image(self._runtime_for(self.account_a), claimed)

        self.assertEqual(error.exception.code, "provider_rejected")
        self.assertFalse(error.exception.request_may_have_been_sent)
        self.assertEqual(error.exception.safe_diagnostics, {
            "publish_stage": "container_create",
            "provider_http_status": 502,
            "provider_error_classification": "provider_error_unparseable",
        })
        self.assertNotIn(provider_secret, str(error.exception.safe_diagnostics))

    def test_cli_runs_a_bounded_execution_and_prints_only_safe_counts(self):
        api = self._api()
        from database.cli import register_database_commands

        app = Flask(__name__)
        register_database_commands(app, lambda: None)
        observed = []

        def execute(session_factory, *, limit):
            observed.append((session_factory, limit))
            return api.ContentPublishingSummary(claimed=2, published=1, failed=1, skipped=0)

        with patch("database.cli.database_url_from_environment", return_value="sqlite:///safe.db"), patch(
            "database.cli.create_session_factory", return_value="session-factory"
        ), patch("database.cli.execute_due_scheduled_content", side_effect=execute, create=True):
            result = app.test_cli_runner().invoke(args=["publish-scheduled-content", "--limit", "2"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(observed, [("session-factory", 2)])
        self.assertEqual(result.output.strip(), "scheduled_content_execution claimed=2 published=1 failed=1 skipped=0")


if __name__ == "__main__":
    unittest.main()
