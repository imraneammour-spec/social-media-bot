import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from unittest.mock import Mock, patch

from database.account_runtime import InstagramAccountRuntime
from database.models import Base, InstagramAccount, InstagramPromptRequest, User


class OAuthFollowupStorageTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            user_a = User(email="followup-a@example.test")
            user_b = User(email="followup-b@example.test")
            session.add_all([user_a, user_b])
            session.flush()
            account_a = InstagramAccount(
                user_id=user_a.id,
                instagram_user_id="17841438617459101",
                connection_status="connected",
            )
            account_b = InstagramAccount(
                user_id=user_b.id,
                instagram_user_id="17841438617459102",
                connection_status="connected",
            )
            session.add_all([account_a, account_b])
            session.flush()
            session.add_all([
                InstagramPromptRequest(
                    instagram_account_id=account_a.id,
                    media_id="media-a",
                    comment_id="comment-a",
                    recipient_id="same-recipient",
                    page_url="https://example.test/p/o/a",
                ),
                InstagramPromptRequest(
                    instagram_account_id=account_b.id,
                    media_id="media-b",
                    comment_id="comment-b",
                    recipient_id="same-recipient",
                    page_url="https://example.test/p/o/b",
                ),
            ])
            self.account_a_id = account_a.id
            self.account_b_id = account_b.id

    def tearDown(self):
        self.engine.dispose()

    def test_source_message_is_unique_per_account_and_recipient_is_tenant_scoped(self):
        from database.models import InstagramOAuthFollowupJob

        due = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            session.add_all([
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a_id,
                    source_message_id="same-mid",
                    recipient_id="same-recipient",
                    page_url="https://example.test/p/o/a",
                    run_at=due,
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_b_id,
                    source_message_id="same-mid",
                    recipient_id="same-recipient",
                    page_url="https://example.test/p/o/b",
                    run_at=due,
                ),
            ])
        with self.assertRaises(IntegrityError):
            with self.Session.begin() as session:
                session.add(InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a_id,
                    source_message_id="same-mid",
                    recipient_id="different-recipient",
                    page_url="https://example.test/p/o/a",
                    run_at=due,
                ))

    def test_schedule_claim_cancel_retry_and_lease_reap_are_account_scoped(self):
        from database.oauth_followups import (
            cancel_oauth_followups,
            claim_due_oauth_followups,
            finalize_oauth_followup_failure,
            finalize_oauth_followup_success,
            reap_expired_oauth_followup_leases,
            schedule_oauth_followup,
        )

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            first = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="mid-a",
                recipient_id="same-recipient",
                page_url="https://example.test/p/o/a",
                run_at=now - timedelta(seconds=1),
            )
            replay = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="mid-a",
                recipient_id="same-recipient",
                page_url="https://example.test/p/o/a",
                run_at=now,
            )
            other_account = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_b_id,
                source_message_id="mid-b",
                recipient_id="same-recipient",
                page_url="https://example.test/p/o/b",
                run_at=now - timedelta(seconds=1),
            )
            self.assertEqual(first.id, replay.id)
            self.assertNotEqual(first.id, other_account.id)

        with self.Session.begin() as session:
            claimed = claim_due_oauth_followups(session, now=now, limit=10)
            self.assertEqual({job.id for job in claimed}, {first.id, other_account.id})
            self.assertTrue(all(job.state == "processing" and job.lease_token for job in claimed))
            claimed_by_id = {job.id: job for job in claimed}
            finalize_oauth_followup_success(session, first.id, claimed_by_id[first.id].lease_token)
            finalize_oauth_followup_failure(
                session, other_account.id, claimed_by_id[other_account.id].lease_token,
                category="credential_unavailable", retryable=True, now=now,
            )

        with self.Session.begin() as session:
            cancelled = cancel_oauth_followups(
                session, instagram_account_id=self.account_a_id, recipient_id="same-recipient", now=now,
            )
            self.assertEqual(cancelled, 0)  # already sent, so cancellation cannot rewrite history.
            self.assertEqual(
                cancel_oauth_followups(
                    session, instagram_account_id=self.account_b_id, recipient_id="same-recipient", now=now,
                ),
                1,
            )

        with self.Session() as session:
            self.assertEqual(claim_due_oauth_followups(session, now=now + timedelta(days=1), limit=10), [])

        with self.Session.begin() as session:
            lease_job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="mid-lease",
                recipient_id="lease-recipient",
                page_url="https://example.test/p/o/a",
                run_at=now - timedelta(seconds=1),
            )
            claim_due_oauth_followups(session, now=now, limit=10)
            self.assertEqual(reap_expired_oauth_followup_leases(session, now=now + timedelta(minutes=16)), 1)
            self.assertEqual(lease_job.state, "failed")

    def test_schedule_rejects_a_prompt_request_owned_by_another_account(self):
        from database.oauth_followups import schedule_oauth_followup

        with self.Session() as session:
            foreign_request = session.query(InstagramPromptRequest).filter_by(
                instagram_account_id=self.account_b_id
            ).one()
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                prompt_request_id=foreign_request.id,
                source_message_id="foreign-request-mid",
                recipient_id="foreign-request-recipient",
                page_url="https://example.test/p/o/a",
            )
        self.assertIsNone(job)

    def test_general_message_followup_is_deduped_without_a_prompt_page_url(self):
        """A direct-message flow may persist explicit follow-up text without prompt metadata."""

        from database.oauth_followups import claim_due_oauth_followups, schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            job = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="general-mid",
                recipient_id="general-recipient",
                page_url=None,
                message_text="Configured general follow-up",
                run_at=now - timedelta(seconds=1),
            )
            replay = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="general-mid",
                recipient_id="general-recipient",
                page_url=None,
                message_text="Changed text must not duplicate",
                run_at=now,
            )
            empty = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="empty-general-mid",
                recipient_id="empty-general-recipient",
                page_url=None,
                message_text="",
            )

            self.assertIsNotNone(job)
            self.assertEqual(job.id, replay.id)
            self.assertIsNone(job.page_url)
            self.assertEqual(job.message_text, "Configured general follow-up")
            self.assertIsNone(empty)

        with self.Session.begin() as session:
            claimed = claim_due_oauth_followups(session, now=now, limit=10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].message_text, "Configured general follow-up")

    def test_response_triggered_job_carries_window_expiry_and_rejects_late_run(self):
        """A worker must never send a response-triggered DM outside Meta's 24-hour window."""
        from database.oauth_followups import schedule_oauth_followup

        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            valid = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="reply-in-window",
                recipient_id="response-window-recipient",
                message_text="Configured follow-up",
                run_at=now + timedelta(minutes=5),
                provider_window_expires_at=now + timedelta(hours=24),
            )
            late = schedule_oauth_followup(
                session,
                instagram_account_id=self.account_a_id,
                source_message_id="reply-outside-window",
                recipient_id="late-window-recipient",
                message_text="Configured follow-up",
                run_at=now + timedelta(hours=24, seconds=1),
                provider_window_expires_at=now + timedelta(hours=24),
            )

        self.assertIsNotNone(valid)
        self.assertEqual(valid.provider_window_expires_at, now + timedelta(hours=24))
        self.assertIsNone(late)


class OAuthFollowupGraphTest(unittest.TestCase):
    def test_expiry_is_rechecked_after_credential_resolution_before_graph(self):
        """A credential lookup crossing the deadline must not dispatch a late request."""
        from database import oauth_graph

        account_id = __import__("uuid").uuid4()
        before_lookup = datetime.now(timezone.utc)
        expiry = before_lookup + timedelta(seconds=1)
        clock = [before_lookup]
        runtime = InstagramAccountRuntime(account_id, "test-account", "oauth", "test-token")
        def resolve(*args, **kwargs):
            clock[0] = expiry
            return runtime
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", side_effect=resolve
        ), patch.object(oauth_graph.requests, "post") as post, patch.object(oauth_graph, "datetime") as mock_clock:
            mock_clock.now.side_effect = lambda zone: clock[0]
            outcome = oauth_graph.send_oauth_delayed_followup(
                lambda: nullcontext(object()), instagram_account_id=account_id,
                instagram_user_id="test-account", recipient_id="test-recipient",
                message_text="Configured follow-up", provider_window_expires_at=expiry,
            )
        self.assertEqual(outcome, "outside_provider_window")
        post.assert_not_called()

    def test_delayed_followup_uses_explicit_general_message_text(self):
        """A general follow-up must never be replaced by legacy prompt wording."""

        from database import oauth_graph

        account_id = __import__("uuid").uuid4()
        response = Mock(ok=True)
        runtime = InstagramAccountRuntime(account_id, "17841438617459211", "oauth", "oauth-token")
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", return_value=runtime
        ), patch.object(oauth_graph.requests, "post", return_value=response) as graph_post:
            outcome = oauth_graph.send_oauth_delayed_followup(
                lambda: nullcontext(object()),
                instagram_account_id=account_id,
                instagram_user_id="17841438617459211",
                recipient_id="recipient-general",
                message_text="Configured general follow-up",
            )

        self.assertEqual(outcome, "success")
        self.assertEqual(
            graph_post.call_args.kwargs["json"]["message"]["text"],
            "Configured general follow-up",
        )

    def test_delayed_followup_uses_only_the_resolved_oauth_runtime_and_sanitizes_outcomes(self):
        from database import oauth_graph

        account_a, account_b = __import__("uuid").uuid4(), __import__("uuid").uuid4()
        response = Mock(ok=True)
        runtime_a = InstagramAccountRuntime(account_a, "17841438617459201", "oauth", "oauth-token-a")
        runtime_b = InstagramAccountRuntime(account_b, "17841438617459202", "oauth", "oauth-token-b")
        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", side_effect=[runtime_a, runtime_b]
        ), patch.object(oauth_graph.requests, "post", return_value=response) as graph_post:
            self.assertEqual(
                oauth_graph.send_oauth_delayed_followup(lambda: nullcontext(object()), instagram_account_id=account_a, instagram_user_id="17841438617459201", recipient_id="recipient-a"),
                "success",
            )
            self.assertEqual(
                oauth_graph.send_oauth_delayed_followup(lambda: nullcontext(object()), instagram_account_id=account_b, instagram_user_id="17841438617459202", recipient_id="recipient-b"),
                "success",
            )
        self.assertEqual(graph_post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer oauth-token-a")
        self.assertEqual(graph_post.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer oauth-token-b")

        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="active"), patch.object(
            oauth_graph, "resolve_oauth_instagram_account_runtime", side_effect=oauth_graph.RuntimeCredentialUnavailableError("secret")
        ), patch.object(oauth_graph.requests, "post") as graph_post:
            self.assertEqual(
                oauth_graph.send_oauth_delayed_followup(lambda: nullcontext(object()), instagram_account_id=account_a, instagram_user_id="17841438617459201", recipient_id="recipient-a"),
                "retryable_pre_dispatch",
            )
        graph_post.assert_not_called()

    def test_non_active_followup_never_dispatches_and_reports_a_terminal_capability_failure(self):
        from database import oauth_graph

        with patch.object(oauth_graph, "oauth_engagement_capability_state", return_value="reconnect_required"), patch.object(
            oauth_graph.requests, "post"
        ) as graph_post:
            outcome = oauth_graph.send_oauth_delayed_followup(
                lambda: nullcontext(object()),
                instagram_account_id=__import__("uuid").uuid4(),
                instagram_user_id="17841438617459203",
                recipient_id="recipient-c",
            )

        self.assertEqual(outcome, "capability_unavailable")
        graph_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
