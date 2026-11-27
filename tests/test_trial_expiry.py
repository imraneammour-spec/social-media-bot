"""60-day free-trial enforcement: expiry, legacy-user grace, reminders.

Covers database/billing.py resolve_trial_start() /
send_trial_reminders() and the User.trial_started_at column added by
migration 20261127_01: a new trial stays "trial", a 61-day-old trial
becomes "trial_expired" with paid features blocked, a workspace with no
stored start is stamped with "today" (deploy date, never created_at),
and the day-45 / day-55 reminders are due exactly on those days and
sent at most once each.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.billing import (
    BillingLimitError,
    check_account_limit,
    consume_usage,
    effective_plan,
    resolve_trial_start,
    send_trial_reminders,
    trial_days_remaining,
    trial_reminder_due,
    usage_snapshot,
)
from database.bootstrap import bootstrap_current_owner
from database.models import Base, BillingSubscription, User


def _days_ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


class TrialExpiryTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'trial.db'}"
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _add_user(self, *, trial_started_at=None, email="user@example.invalid"):
        with self.Session.begin() as session:
            user = User(email=email, trial_started_at=trial_started_at)
            session.add(user)
            session.flush()  # populate the uuid id before returning it
            return user.id

    def _add_paid_subscription(self, user_id):
        with self.Session.begin() as session:
            session.add(BillingSubscription(
                user_id=user_id, plan="pro", status="active",
                test_mode=False,
                renews_at=datetime.now(timezone.utc) + timedelta(days=30),
            ))

    def test_new_user_trial_starts_today_and_stays_trial(self):
        user_id = self._add_user(trial_started_at=datetime.now(timezone.utc))
        with self.Session.begin() as session:
            user = session.get(User, user_id)
            self.assertEqual(
                effective_plan(None, trial_started_at=user.trial_started_at), "trial"
            )
            self.assertEqual(trial_days_remaining(user.trial_started_at), 60)
            consume_usage(session, user_id, "content")  # within the 30/mo trial limit
            snapshot = {
                row["metric"]: row for row in usage_snapshot(session, user_id)
            }
            self.assertEqual(snapshot["content"]["used"], 1)
            self.assertEqual(snapshot["content"]["limit"], 30)

    def test_trial_started_61_days_ago_loses_trial_and_blocks_paid_features(self):
        user_id = self._add_user(trial_started_at=_days_ago(61))
        with self.Session.begin() as session:
            user = session.get(User, user_id)
            self.assertEqual(
                effective_plan(None, trial_started_at=user.trial_started_at),
                "trial_expired",
            )
            self.assertEqual(trial_days_remaining(user.trial_started_at), 0)
            with self.assertRaises(BillingLimitError):
                consume_usage(session, user_id, "content")
            with self.assertRaises(BillingLimitError):
                consume_usage(session, user_id, "ai")
            with self.assertRaises(BillingLimitError):
                check_account_limit(session, user_id)
            limits = {
                row["metric"]: row["limit"]
                for row in usage_snapshot(session, user_id)
            }
            self.assertEqual(limits, {"accounts": 0, "content": 0, "ai": 0})
            # No data is deleted: the user row itself is untouched.
            self.assertIsNotNone(session.get(User, user_id))

    def test_trial_boundary_day_59_is_trial_day_60_is_expired(self):
        self.assertEqual(
            effective_plan(None, trial_started_at=_days_ago(59)), "trial"
        )
        self.assertEqual(
            effective_plan(None, trial_started_at=_days_ago(60)), "trial_expired"
        )

    def test_user_without_trial_start_is_stamped_with_today(self):
        user_id = self._add_user(trial_started_at=None)
        with self.Session.begin() as session:
            stamped = resolve_trial_start(session, user_id)
            self.assertIsNotNone(stamped)
            # Deploy-date rule: the stamp is ~now, not the account's age.
            self.assertLess(
                abs((datetime.now(timezone.utc) - stamped).total_seconds()), 60
            )
            self.assertEqual(
                effective_plan(None, trial_started_at=stamped), "trial"
            )
            self.assertEqual(trial_days_remaining(stamped), 60)
        with self.Session.begin() as session:
            # The stamp persisted, and a second resolve returns the same day.
            self.assertEqual(
                resolve_trial_start(session, user_id),
                session.get(User, user_id).trial_started_at,
            )

    def test_usage_snapshot_does_not_stamp_a_read_only_start(self):
        user_id = self._add_user(trial_started_at=None)
        with self.Session() as session:
            usage_snapshot(session, user_id)
            self.assertIsNone(session.get(User, user_id).trial_started_at)
        # The next (write) call stamps it for real.
        with self.Session.begin() as session:
            consume_usage(session, user_id, "ai")
        with self.Session() as session:
            self.assertIsNotNone(session.get(User, user_id).trial_started_at)

    def test_bootstrap_stamps_trial_start_for_new_owner(self):
        with self.Session.begin() as session:
            result = bootstrap_current_owner(
                session, owner_email="owner@example.invalid",
                owner_name="Owner", instagram_user_id="123",
            )
        self.assertTrue(result.user_created)
        self.assertIsNotNone(result.user.trial_started_at)
        self.assertLess(
            abs((datetime.now(timezone.utc) - result.user.trial_started_at).total_seconds()),
            60,
        )

    def test_reminder_due_on_day_45_and_55_only(self):
        self.assertEqual(trial_reminder_due(_days_ago(45)), 45)
        self.assertEqual(trial_reminder_due(_days_ago(55)), 55)
        self.assertIsNone(trial_reminder_due(_days_ago(44)))
        self.assertIsNone(trial_reminder_due(_days_ago(46)))
        self.assertIsNone(trial_reminder_due(_days_ago(54)))
        self.assertIsNone(trial_reminder_due(_days_ago(56)))
        self.assertIsNone(trial_reminder_due(_days_ago(61)))  # expired: no reminder
        self.assertIsNone(trial_reminder_due(None))

    def test_reminders_sent_once_each_and_skipped_when_not_due(self):
        user_id = self._add_user(
            trial_started_at=_days_ago(45), email="due@example.invalid"
        )
        calls = []

        def fake_sender(user, days_remaining):
            calls.append((user.id, days_remaining))
            return True

        result = send_trial_reminders(self.Session, sender=fake_sender)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["due_unsent"], 0)
        self.assertEqual(calls, [(user_id, 15)])
        # Second run: the day-45 marker is recorded, nothing is resent.
        result = send_trial_reminders(self.Session, sender=fake_sender)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(calls, [(user_id, 15)])
        with self.Session() as session:
            preferences = session.get(User, user_id).notification_preferences
            self.assertIn("trial_reminder_45_sent_at", preferences)

    def test_reminders_skip_expired_and_paid_users(self):
        self._add_user(trial_started_at=_days_ago(61), email="old@example.invalid")
        paid_id = self._add_user(
            trial_started_at=_days_ago(45), email="paid@example.invalid"
        )
        self._add_paid_subscription(paid_id)
        self._add_user(trial_started_at=_days_ago(10), email="fresh@example.invalid")
        calls = []

        def fake_sender(user, days_remaining):
            calls.append(user.id)
            return True

        result = send_trial_reminders(self.Session, sender=fake_sender)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(result["skipped"], 2)  # expired trial + paid plan
        self.assertEqual(result["checked"], 3)

    def test_reminders_without_sender_are_due_unsent_not_marked(self):
        user_id = self._add_user(
            trial_started_at=_days_ago(55), email="nosmtp@example.invalid"
        )
        result = send_trial_reminders(self.Session, sender=None)
        self.assertEqual(result["sent"], 0)
        # Day 55 means both milestones (45 and 55) are due and unsent.
        self.assertEqual(result["due_unsent"], 2)
        with self.Session() as session:
            preferences = session.get(User, user_id).notification_preferences or {}
            self.assertNotIn("trial_reminder_45_sent_at", preferences)
            self.assertNotIn("trial_reminder_55_sent_at", preferences)

    def test_reminder_sender_failure_is_not_marked_sent(self):
        user_id = self._add_user(
            trial_started_at=_days_ago(45), email="fail@example.invalid"
        )
        result = send_trial_reminders(self.Session, sender=lambda u, d: False)
        self.assertEqual(result["sent"], 0)
        self.assertEqual(result["due_unsent"], 1)
        with self.Session() as session:
            preferences = session.get(User, user_id).notification_preferences or {}
            self.assertNotIn("trial_reminder_45_sent_at", preferences)


if __name__ == "__main__":
    unittest.main()
