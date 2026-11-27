import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database.content import (
    CONTENT_TYPES,
    create_content_draft,
    delete_content_draft,
    list_content_drafts,
    mark_content_ready,
    normalize_content_type,
    recover_failed_content_draft,
    update_content_draft,
)
from database.models import Base, InstagramAccount, InstagramContentItem, User


class Phase7AContentFoundationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase7a-content.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._seed_accounts()

    def tearDown(self):
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_accounts(self):
        with self.session_factory.begin() as session:
            owner_a = User(email="owner-a@example.invalid", display_name="Owner A")
            owner_b = User(email="owner-b@example.invalid", display_name="Owner B")
            session.add_all([owner_a, owner_b])
            session.flush()
            self.account_a = InstagramAccount(
                user_id=owner_a.id,
                instagram_user_id="10000000000000001",
                username="alpha_account",
                connection_status="connected",
            )
            self.account_b = InstagramAccount(
                user_id=owner_b.id,
                instagram_user_id="20000000000000002",
                username="bravo_account",
                connection_status="connected",
            )
            session.add_all([self.account_a, self.account_b])
            session.flush()
            self.account_a_id = self.account_a.id
            self.account_b_id = self.account_b.id

    def test_create_draft_and_list_are_account_scoped(self):
        with self.session_factory.begin() as session:
            draft = create_content_draft(
                session,
                instagram_account_id=self.account_a_id,
                caption="Draft one",
                content_type="image",
                media_url="https://images.example.com/a.jpg",
            )
            self.assertIsInstance(draft, InstagramContentItem)
            self.assertEqual(draft.status, "draft")
            self.assertEqual(draft.content_type, "image")

            other = create_content_draft(
                session,
                instagram_account_id=self.account_b_id,
                caption="Other draft",
                content_type="image",
                media_url="https://images.example.com/b.jpg",
            )
            self.assertEqual(other.instagram_account_id, self.account_b_id)

            account_a_rows = list_content_drafts(session, self.account_a_id)
            account_b_rows = list_content_drafts(session, self.account_b_id)

            self.assertEqual(len(account_a_rows), 1)
            self.assertEqual(len(account_b_rows), 1)
            self.assertEqual(account_a_rows[0].caption, "Draft one")
            self.assertEqual(account_b_rows[0].caption, "Other draft")

    def test_mark_ready_and_return_to_draft(self):
        with self.session_factory.begin() as session:
            draft = create_content_draft(
                session,
                instagram_account_id=self.account_a_id,
                caption="Ready me",
                content_type="image",
                media_url="https://images.example.com/ready.jpg",
            )
            ready = mark_content_ready(session, self.account_a_id, draft.id)
            self.assertEqual(ready.status, "ready")

            updated = update_content_draft(
                session,
                self.account_a_id,
                ready.id,
                status="draft",
                caption="Edited again",
            )
            self.assertEqual(updated.status, "draft")
            self.assertEqual(updated.caption, "Edited again")

    def test_invalid_type_and_media_url_are_rejected(self):
        with self.session_factory.begin() as session:
            with self.assertRaises(ValueError):
                create_content_draft(
                    session,
                    instagram_account_id=self.account_a_id,
                    caption="Bad type",
                    content_type="story",
                    media_url="https://images.example.com/a.jpg",
                )
            with self.assertRaises(ValueError):
                create_content_draft(
                    session,
                    instagram_account_id=self.account_a_id,
                    caption="Bad URL",
                    content_type="image",
                    media_url="ftp://images.example.com/a.jpg",
                )
            with self.assertRaises(ValueError):
                create_content_draft(
                    session,
                    instagram_account_id=self.account_a_id,
                    caption="x" * 2201,
                    content_type="image",
                    media_url="https://images.example.com/a.jpg",
                )

    def test_cross_account_edit_and_delete_are_blocked(self):
        with self.session_factory.begin() as session:
            draft = create_content_draft(
                session,
                instagram_account_id=self.account_a_id,
                caption="Account A draft",
                content_type="image",
                media_url="https://images.example.com/a.jpg",
            )

            with self.assertRaises(PermissionError):
                update_content_draft(
                    session,
                    self.account_b_id,
                    draft.id,
                    caption="Attempted edit",
                )

            with self.assertRaises(PermissionError):
                delete_content_draft(session, self.account_b_id, draft.id)

    def test_supported_content_types_are_image_and_reel_in_v2(self):
        self.assertEqual(normalize_content_type("Image"), "image")
        self.assertEqual(normalize_content_type("Reel"), "reel")
        self.assertEqual(CONTENT_TYPES, ("image", "reel"))
        for unsupported in ("video", "carousel"):
            with self.subTest(content_type=unsupported):
                with self.assertRaises(ValueError):
                    normalize_content_type(unsupported)

    def test_recover_failed_content_draft_returns_owned_failed_item_to_draft(self):
        with self.session_factory.begin() as session:
            failed = InstagramContentItem(
                instagram_account_id=self.account_a_id,
                status="failed",
                content_type="image",
                caption="Recover me",
                media_url="https://images.example.com/recover.jpg",
                cover_url="https://images.example.com/recover-cover.jpg",
                alt_text="Recover alt",
                metadata_json={"content_type": "image", "keep": True},
                scheduled_for=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
                publish_error_code="provider_rejected",
                provider_request_may_have_been_sent=True,
            )
            session.add(failed)
            session.flush()

            recovered = recover_failed_content_draft(session, self.account_a_id, failed.id)

            self.assertEqual(recovered.status, "draft")
            # Caption / media URL / metadata are preserved for the Draft editor.
            self.assertEqual(recovered.caption, "Recover me")
            self.assertEqual(recovered.media_url, "https://images.example.com/recover.jpg")
            self.assertEqual(recovered.cover_url, "https://images.example.com/recover-cover.jpg")
            self.assertEqual(recovered.alt_text, "Recover alt")
            self.assertEqual(recovered.metadata_json, {"content_type": "image", "keep": True})
            # Scheduled/publishing lifecycle metadata must not linger on a Draft.
            self.assertIsNone(recovered.scheduled_for)
            self.assertIsNone(recovered.publish_error_code)
            self.assertIsNone(recovered.published_at)
            self.assertIsNone(recovered.provider_media_id)
            self.assertFalse(recovered.provider_request_may_have_been_sent)

    def test_recover_failed_content_draft_rejects_nonfailed_and_cross_account(self):
        with self.session_factory.begin() as session:
            failed = InstagramContentItem(
                instagram_account_id=self.account_a_id,
                status="failed",
                content_type="image",
                caption="Keep failed",
                media_url="https://images.example.com/failed.jpg",
                scheduled_for=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
                publish_error_code="provider_rejected",
            )
            ready = InstagramContentItem(
                instagram_account_id=self.account_a_id,
                status="ready",
                content_type="image",
                caption="Ready row",
                media_url="https://images.example.com/ready.jpg",
            )
            scheduled = InstagramContentItem(
                instagram_account_id=self.account_a_id,
                status="scheduled",
                content_type="image",
                caption="Scheduled row",
                media_url="https://images.example.com/scheduled.jpg",
                scheduled_for=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
            )
            published = InstagramContentItem(
                instagram_account_id=self.account_a_id,
                status="published",
                content_type="image",
                caption="Published row",
                media_url="https://images.example.com/published.jpg",
                published_at=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc),
                provider_media_id="published-provider-id",
            )
            session.add_all([failed, ready, scheduled, published])
            session.flush()
            failed_id = failed.id
            ready_id = ready.id
            scheduled_id = scheduled.id
            published_id = published.id

        # Healthy states are not recoverable; only the terminal failed state is.
        for item_id in (ready_id, scheduled_id, published_id):
            with self.session_factory() as session:
                with self.assertRaises(ValueError):
                    recover_failed_content_draft(session, self.account_a_id, item_id)

        # Another account cannot recover an item it does not own.
        with self.session_factory() as session:
            with self.assertRaises(PermissionError):
                recover_failed_content_draft(session, self.account_b_id, failed_id)

        with self.session_factory() as session:
            failed = session.get(InstagramContentItem, failed_id)
            ready = session.get(InstagramContentItem, ready_id)
            scheduled = session.get(InstagramContentItem, scheduled_id)
            published = session.get(InstagramContentItem, published_id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.publish_error_code, "provider_rejected")
            self.assertEqual(ready.status, "ready")
            self.assertEqual(scheduled.status, "scheduled")
            self.assertEqual(published.status, "published")
            self.assertEqual(published.provider_media_id, "published-provider-id")


if __name__ == "__main__":
    unittest.main()
