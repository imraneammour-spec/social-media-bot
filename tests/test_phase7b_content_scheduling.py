import tempfile
import unittest
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.content import (
    create_content_draft,
    mark_content_ready,
    schedule_content_draft,
    unschedule_content_draft,
)
from database.models import Base, InstagramAccount, InstagramContentItem, User
from tests.test_messages_content import MessagesContentDashboardTest
from tenant_dashboard import _content_draft_reference


class Phase7BContentSchedulingTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase7b.db'}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session.begin() as session:
            owner_a, owner_b = User(email="schedule-a@test"), User(email="schedule-b@test")
            session.add_all([owner_a, owner_b]); session.flush()
            self.account_a = InstagramAccount(user_id=owner_a.id, instagram_user_id="71000000000000001", username="schedule_a", connection_status="connected")
            self.account_b = InstagramAccount(user_id=owner_b.id, instagram_user_id="72000000000000002", username="schedule_b", connection_status="connected")
            session.add_all([self.account_a, self.account_b]); session.flush()

    def tearDown(self):
        self.engine.dispose(); self.tempdir.cleanup()

    def _draft(self, session, account):
        return create_content_draft(session, instagram_account_id=account.id, caption="Scheduled content", content_type="image", media_url="https://example.com/content.jpg")

    def test_ready_content_schedules_in_utc_and_returns_to_ready(self):
        casablanca_time = datetime.now(ZoneInfo("Africa/Casablanca")) + timedelta(days=2)
        with self.Session.begin() as session:
            draft = self._draft(session, self.account_a)
            mark_content_ready(session, self.account_a.id, draft.id)
            schedule_content_draft(session, self.account_a.id, draft.id, casablanca_time)
            self.assertEqual(draft.status, "scheduled")
            self.assertEqual(draft.scheduled_for, casablanca_time.astimezone(timezone.utc))
            unschedule_content_draft(session, self.account_a.id, draft.id)
            self.assertEqual(draft.status, "ready")
            self.assertIsNone(draft.scheduled_for)

    def test_draft_past_and_cross_account_scheduling_are_rejected_without_mutation(self):
        future = datetime.now(timezone.utc) + timedelta(days=1)
        with self.Session.begin() as session:
            draft = self._draft(session, self.account_a)
            with self.assertRaises(ValueError):
                schedule_content_draft(session, self.account_a.id, draft.id, future)
            self.assertEqual(draft.status, "draft")
            mark_content_ready(session, self.account_a.id, draft.id)
            with self.assertRaises(PermissionError):
                schedule_content_draft(session, self.account_b.id, draft.id, future)
            with self.assertRaises(ValueError):
                schedule_content_draft(session, self.account_a.id, draft.id, datetime.now(timezone.utc) - timedelta(minutes=1))
            self.assertEqual(draft.status, "ready")
            self.assertIsNone(draft.scheduled_for)

    def test_unscheduling_requires_the_owning_account_and_scheduled_state(self):
        with self.Session.begin() as session:
            draft = self._draft(session, self.account_a)
            with self.assertRaises(ValueError):
                unschedule_content_draft(session, self.account_a.id, draft.id)
            mark_content_ready(session, self.account_a.id, draft.id)
            schedule_content_draft(session, self.account_a.id, draft.id, datetime.now(timezone.utc) + timedelta(days=1))
            with self.assertRaises(PermissionError):
                unschedule_content_draft(session, self.account_b.id, draft.id)
            self.assertEqual(draft.status, "scheduled")

    def test_dashboard_schedule_and_unschedule_use_rendered_csrf_and_opaque_references(self):
        """The browser flow never needs raw content or account identifiers."""
        case = MessagesContentDashboardTest("test_content_empty_state_works")
        case.setUp()
        self.addCleanup(case.tearDown)
        case._login_as(case.owner_a)
        account_a_ref = case._reference(case.owner_a, case.account_a)
        account_b_ref = case._reference(case.owner_b, case.account_b)
        with case.Session.begin() as session:
            ready = create_content_draft(session, instagram_account_id=case.account_a.id, caption="UI ready", content_type="image", media_url="https://example.com/ui-ready.jpg")
            mark_content_ready(session, case.account_a.id, ready.id)
            draft = create_content_draft(session, instagram_account_id=case.account_a.id, caption="UI draft", content_type="image", media_url="https://example.com/ui-draft.jpg")
            foreign = create_content_draft(session, instagram_account_id=case.account_b.id, caption="Foreign schedule", content_type="image", media_url="https://example.com/foreign.jpg")
            mark_content_ready(session, case.account_b.id, foreign.id)

        page = case.client.get(f"/app/content?account={account_a_ref}").get_data(as_text=True)
        self.assertNotIn(str(ready.id), page)
        self.assertNotIn(str(case.account_a.id), page)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        schedule_ref = re.search(r'action="/app/content/drafts/schedule\?account=[^"]+".*?name="draft_ref" value="([^"]+)"', page, re.DOTALL).group(1)
        with case.app.app_context():
            edit_ref = _content_draft_reference(
                case.owner_a.id,
                ready.id,
                purpose="content-draft-edit",
            )
        future_date = (datetime.now(ZoneInfo("Africa/Casablanca")) + timedelta(days=2)).strftime("%Y-%m-%d")
        response = case.client.post(f"/app/content/drafts/schedule?account={account_a_ref}", data={"csrf_token": csrf, "draft_ref": schedule_ref, "date": future_date, "time": "12:30"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        with case.Session.begin() as session:
            item = session.get(InstagramContentItem, ready.id)
            self.assertEqual(item.status, "scheduled")
            self.assertIsNotNone(item.scheduled_for)
            scheduled_for = item.scheduled_for

        self.assertEqual(case.client.post(f"/app/content/drafts/edit?account={account_a_ref}", data={"csrf_token": csrf, "draft_ref": edit_ref, "caption": "mutated", "content_type": "video", "media_url": "https://example.com/mutated.mp4"}, follow_redirects=False).status_code, 400)
        with case.Session.begin() as session:
            item = session.get(InstagramContentItem, ready.id)
            self.assertEqual((item.caption, item.content_type, item.media_url, item.status, item.scheduled_for), ("UI ready", "image", "https://example.com/ui-ready.jpg", "scheduled", scheduled_for))

        scheduled_page = case.client.get(f"/app/content?account={account_a_ref}").get_data(as_text=True)
        scheduled_card = re.search(r"Scheduled.*?UI ready.*?</article>", scheduled_page, re.DOTALL).group(0)
        self.assertNotIn('aria-label="Edit draft"', scheduled_card)
        unschedule_ref = re.search(r'action="/app/content/drafts/unschedule\?account=[^"]+".*?name="draft_ref" value="([^"]+)"', scheduled_page, re.DOTALL).group(1)
        self.assertEqual(case.client.post(f"/app/content/drafts/unschedule?account={account_a_ref}", data={"csrf_token": csrf, "draft_ref": unschedule_ref}, follow_redirects=False).status_code, 303)

        invalid_unschedule = case.client.post(f"/app/content/drafts/unschedule?account={account_a_ref}", data={"csrf_token": csrf, "draft_ref": "invalid"}, follow_redirects=False)
        self.assertEqual(invalid_unschedule.status_code, 400)

        invalid = case.client.post(f"/app/content/drafts/schedule?account={account_a_ref}", data={"csrf_token": csrf, "draft_ref": "invalid", "date": future_date, "time": "12:30"}, follow_redirects=False)
        self.assertEqual(invalid.status_code, 400)
        csrf_failure = case.client.post(f"/app/content/drafts/schedule?account={account_a_ref}", data={"csrf_token": "bad", "draft_ref": schedule_ref, "date": future_date, "time": "12:30"}, follow_redirects=False)
        self.assertEqual(csrf_failure.status_code, 400)
        self.assertEqual(case.client.post(f"/app/content/drafts/schedule?account={account_b_ref}", data={"csrf_token": csrf, "draft_ref": schedule_ref, "date": future_date, "time": "12:30"}, follow_redirects=False).status_code, 404)
        scheduler_a = case.client.get(f"/app/scheduler?account={account_a_ref}").get_data(as_text=True)
        scheduler_b = case.client.get(f"/app/scheduler?account={account_b_ref}").get_data(as_text=True)
        self.assertIn("Content schedule", scheduler_a)
        self.assertIn("Follow-up", scheduler_a)
        self.assertNotIn("UI ready", scheduler_b)
        with case.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, draft.id).status, "draft")
            self.assertEqual(session.get(InstagramContentItem, foreign.id).status, "ready")

    def test_expired_schedule_and_unschedule_references_leave_content_unchanged(self):
        """Expired purpose-bound browser references are rejected without waiting or mutation."""
        case = MessagesContentDashboardTest("test_content_empty_state_works")
        case.setUp()
        self.addCleanup(case.tearDown)
        case._login_as(case.owner_a)
        account_ref = case._reference(case.owner_a, case.account_a)
        expires_before_now = int(datetime.now(timezone.utc).timestamp()) - 901
        with case.Session.begin() as session:
            ready = create_content_draft(session, instagram_account_id=case.account_a.id, caption="Expired ready", content_type="image", media_url="https://example.com/expired-ready.jpg")
            mark_content_ready(session, case.account_a.id, ready.id)
            scheduled = create_content_draft(session, instagram_account_id=case.account_a.id, caption="Expired scheduled", content_type="image", media_url="https://example.com/expired-scheduled.jpg")
            mark_content_ready(session, case.account_a.id, scheduled.id)
            schedule_content_draft(session, case.account_a.id, scheduled.id, datetime.now(timezone.utc) + timedelta(days=1))
            scheduled_for = scheduled.scheduled_for
        with case.app.app_context():
            expired_schedule_ref = _content_draft_reference(case.owner_a.id, ready.id, purpose="content-schedule", now=expires_before_now)
            expired_unschedule_ref = _content_draft_reference(case.owner_a.id, scheduled.id, purpose="content-unschedule", now=expires_before_now)
        page = case.client.get(f"/app/content?account={account_ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        future_date = (datetime.now(ZoneInfo("Africa/Casablanca")) + timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertEqual(case.client.post(f"/app/content/drafts/schedule?account={account_ref}", data={"csrf_token": csrf, "draft_ref": expired_schedule_ref, "date": future_date, "time": "12:30"}, follow_redirects=False).status_code, 400)
        self.assertEqual(case.client.post(f"/app/content/drafts/unschedule?account={account_ref}", data={"csrf_token": csrf, "draft_ref": expired_unschedule_ref}, follow_redirects=False).status_code, 400)
        with case.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, ready.id).status, "ready")
            self.assertIsNone(session.get(InstagramContentItem, ready.id).scheduled_for)
            self.assertEqual(session.get(InstagramContentItem, scheduled.id).status, "scheduled")
            persisted_time = session.get(InstagramContentItem, scheduled.id).scheduled_for
            self.assertEqual(persisted_time.replace(tzinfo=timezone.utc), scheduled_for)
