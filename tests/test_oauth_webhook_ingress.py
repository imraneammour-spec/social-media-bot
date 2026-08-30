import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from database.models import (
    Base,
    InstagramAccount,
    InstagramOAuthWebhookWorkItem,
    User,
)
from database.oauth_webhook_ingress import (
    claim_due_oauth_webhook_work,
    complete_oauth_webhook_work,
    enqueue_oauth_webhook_work,
)


class OAuthWebhookIngressTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.tempdir.name) / 'oauth-webhook-ingress.db'}"
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            owner = User(email="ingress-owner@example.test")
            session.add(owner)
            session.flush()
            self.account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458902",
                connection_status="connected",
                provider="instagram",
            )
            self.other_account = InstagramAccount(
                user_id=owner.id,
                instagram_user_id="17841438617458903",
                connection_status="connected",
                provider="instagram",
            )
            session.add_all([self.account, self.other_account])

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _comment(self, event_id="comment-1"):
        return {
            "id": event_id,
            "text": "private comment text",
            "from": {"id": "recipient-1", "username": "lead"},
            "media": {"id": "media-1"},
        }

    def test_enqueue_deduplicates_one_account_scoped_comment_work_item(self):
        with self.Session.begin() as session:
            first = enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.account.id,
                instagram_user_id=self.account.instagram_user_id,
                event_type="comment",
                value=self._comment(),
            )
            second = enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.account.id,
                instagram_user_id=self.account.instagram_user_id,
                event_type="comment",
                value=self._comment(),
            )

        self.assertTrue(first)
        self.assertFalse(second)
        with self.Session() as session:
            items = session.scalars(select(InstagramOAuthWebhookWorkItem)).all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].state, "pending")
        self.assertEqual(items[0].payload["id"], "comment-1")

    def test_expired_lease_is_recoverable_and_completed_work_is_not_reclaimed(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.account.id,
                instagram_user_id=self.account.instagram_user_id,
                event_type="comment",
                value=self._comment(),
            )

        with self.Session.begin() as session:
            first = claim_due_oauth_webhook_work(session, now=now)
        self.assertEqual(len(first), 1)
        with self.Session.begin() as session:
            recovered = claim_due_oauth_webhook_work(
                session, now=now + timedelta(minutes=3)
            )
        self.assertEqual(len(recovered), 1)
        self.assertNotEqual(first[0].lease_token, recovered[0].lease_token)
        with self.Session.begin() as session:
            complete_oauth_webhook_work(
                session, recovered[0].id, recovered[0].lease_token
            )
        with self.Session.begin() as session:
            self.assertEqual(
                claim_due_oauth_webhook_work(
                    session, now=now + timedelta(minutes=6)
                ),
                [],
            )

    def test_same_provider_event_isolated_between_accounts(self):
        with self.Session.begin() as session:
            enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.account.id,
                instagram_user_id=self.account.instagram_user_id,
                event_type="comment",
                value=self._comment("shared-event"),
            )
            enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.other_account.id,
                instagram_user_id=self.other_account.instagram_user_id,
                event_type="comment",
                value=self._comment("shared-event"),
            )

        with self.Session.begin() as session:
            claimed = claim_due_oauth_webhook_work(session)
        self.assertEqual({item.instagram_account_id for item in claimed}, {
            self.account.id,
            self.other_account.id,
        })

    def test_ingress_keeps_only_processor_fields_and_never_raw_envelope_data(self):
        value = self._comment()
        value["access_token"] = "token-that-must-not-persist"
        value["provider_response"] = {"secret": "must-not-persist"}
        with self.Session.begin() as session:
            self.assertTrue(enqueue_oauth_webhook_work(
                session,
                instagram_account_id=self.account.id,
                instagram_user_id=self.account.instagram_user_id,
                event_type="comment",
                value=value,
            ))
        with self.Session() as session:
            item = session.scalar(select(InstagramOAuthWebhookWorkItem))
        stored = str(item.payload)
        self.assertNotIn("token-that-must-not-persist", stored)
        self.assertNotIn("must-not-persist", stored)


if __name__ == "__main__":
    unittest.main()
