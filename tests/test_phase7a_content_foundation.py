import tempfile
import unittest
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
                content_type="reel",
                media_url="https://images.example.com/b.mp4",
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

    def test_supported_content_types_are_normalized(self):
        self.assertEqual(normalize_content_type("Image"), "image")
        self.assertEqual(normalize_content_type("REEL"), "reel")
        self.assertIn("image", CONTENT_TYPES)
        self.assertIn("reel", CONTENT_TYPES)


if __name__ == "__main__":
    unittest.main()
