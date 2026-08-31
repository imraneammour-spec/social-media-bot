"""Phase 5C — Messages & Content dashboard tests.

Verifies account-scoped tenant isolation, filters, pagination, empty states,
and safe URL rendering for the Messages and Content dashboard screens.
"""

import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from auth import (
    action_reference_for_account,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from database.content import create_content_draft
from database.models import (
    Base,
    InstagramAccount,
    InstagramContentItem,
    InstagramCredential,
    InstagramEngagementEvent,
    InstagramOAuthFollowupJob,
    InstagramPromptPost,
    InstagramPromptRequest,
    InstagramSmartReplyActivity,
    User,
)
from tenant_dashboard import _content_draft_reference, register_tenant_dashboard


class MessagesContentDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite+pysqlite:///{Path(self.tempdir.name) / 'phase5c.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        template_dir = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
        static_dir = Path(__file__).resolve().parents[1] / "dashboard" / "static"
        self.app = Flask(
            __name__,
            template_folder=str(template_dir),
            static_folder=str(static_dir),
        )
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="phase5c-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "legacy dashboard")
        self.app.add_url_rule("/api/legacy", "legacy_api", lambda: {"legacy": True})
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_tenant_dashboard(self.app)
        self._seed()
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed(self):
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            self.owner_a = User(email="owner-a@example.invalid", display_name="Owner A")
            self.owner_b = User(email="owner-b@example.invalid", display_name="Owner B")
            session.add_all([self.owner_a, self.owner_b])
            session.flush()
            self.account_a = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000001",
                username="alpha_account",
                connection_status="connected",
            )
            self.account_b = InstagramAccount(
                user_id=self.owner_b.id,
                instagram_user_id="20000000000000002",
                username="bravo_account",
                connection_status="connected",
            )
            session.add_all([self.account_a, self.account_b])
            session.flush()
            session.add_all([
                InstagramCredential(
                    instagram_account_id=self.account_a.id,
                    token_ciphertext="ciphertext-a-secret",
                    encryption_key_id="KEY_2026_01",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                    token_status="valid",
                ),
                InstagramCredential(
                    instagram_account_id=self.account_b.id,
                    token_ciphertext="ciphertext-b-secret",
                    encryption_key_id="KEY_2026_01",
                    scopes=[
                        "instagram_business_basic",
                        "instagram_business_manage_comments",
                        "instagram_business_manage_messages",
                    ],
                    token_status="valid",
                ),
            ])
            # Prompt posts — account A
            session.add_all([
                InstagramPromptPost(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-1-secret",
                    public_id="opaque-prompt-a1",
                    prompts=[{"text": "Prompt A1"}, {"text": "Prompt A1b"}],
                    permalink="https://www.instagram.com/p/public-a1/",
                    cover_url="https://cdn.instagram.com/cover-a1.jpg",
                ),
                InstagramPromptPost(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-2-secret",
                    public_id="opaque-prompt-a2",
                    prompts=[{"text": "Prompt A2"}],
                ),
            ])
            # Prompt post — account B
            session.add(InstagramPromptPost(
                instagram_account_id=self.account_b.id,
                media_id="media-b-1-secret",
                public_id="opaque-prompt-b1",
                prompts=[{"text": "Prompt B1"}],
                permalink="https://www.instagram.com/p/cross-owner-b/",
                cover_url="https://cdn.instagram.com/cross-owner-b.jpg",
            ))
            # Prompt requests — account A
            session.add_all([
                InstagramPromptRequest(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-1-secret",
                    comment_id="comment-a-1-secret",
                    recipient_id="recipient-a-secret",
                    username="user_alpha_one",
                    page_url="https://internal.example.invalid/a1",
                ),
                InstagramPromptRequest(
                    instagram_account_id=self.account_a.id,
                    media_id="media-a-2-secret",
                    comment_id="comment-a-2-secret",
                    recipient_id="recipient-a2-secret",
                    username="user_alpha_two",
                    page_url="https://internal.example.invalid/a2",
                ),
            ])
            # Prompt request — account B
            session.add(InstagramPromptRequest(
                instagram_account_id=self.account_b.id,
                media_id="media-b-1-secret",
                comment_id="comment-b-1-secret",
                recipient_id="recipient-b-secret",
                username="user_bravo_one",
                page_url="https://internal.example.invalid/b1",
            ))
            # Smart replies — account A
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=self.account_a.id,
                comment_id="smart-reply-a-secret",
                recipient_id="recipient-a-secret",
                username="user_alpha_one",
                reply_text="private smart reply content a",
            ))
            # Smart reply — account B
            session.add(InstagramSmartReplyActivity(
                instagram_account_id=self.account_b.id,
                comment_id="smart-reply-b-secret",
                recipient_id="recipient-b-secret",
                username="user_bravo_one",
                reply_text="private smart reply content b",
            ))
            # Engagement events — account A
            session.add_all([
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a.id,
                    provider_event_id="event-a-comment-secret",
                    event_type="comment",
                    state="done",
                ),
                InstagramEngagementEvent(
                    instagram_account_id=self.account_a.id,
                    provider_event_id="event-a-message-secret",
                    event_type="message",
                    state="done",
                ),
            ])
            # Engagement event — account B
            session.add(InstagramEngagementEvent(
                instagram_account_id=self.account_b.id,
                provider_event_id="event-b-comment-secret",
                event_type="comment",
                state="done",
            ))
            # Follow-up jobs — account A
            session.add_all([
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a.id,
                    source_message_id="followup-a-pending-secret",
                    recipient_id="recipient-a-secret",
                    page_url="https://internal.example.invalid/a-followup",
                    state="pending",
                    run_at=now + timedelta(hours=6),
                ),
                InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a.id,
                    source_message_id="followup-a-sent-secret",
                    recipient_id="recipient-a-secret",
                    page_url="https://internal.example.invalid/a-followup",
                    state="sent",
                    run_at=now,
                    sent_at=now,
                ),
            ])
            # Follow-up job — account B
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=self.account_b.id,
                source_message_id="followup-b-pending-secret",
                recipient_id="recipient-b-secret",
                page_url="https://internal.example.invalid/b-followup",
                state="pending",
                run_at=now + timedelta(hours=6),
            ))

    def _login_as(self, user):
        with self.client.session_transaction() as session:
            session["user_id"] = str(user.id)
            session["_csrf_token"] = "csrf-test-token"

    def _reference(self, user, account):
        return action_reference_for_account(
            user.id,
            account.id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
        )

    # ==================================================================
    # MESSAGES TESTS
    # ==================================================================

    def test_messages_selected_account_only_sees_own_interactions(self):
        """1. Selected account only sees its own interactions."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/messages?account={ref}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        usernames = {item["username"] for item in data["messages"] if item["username"]}
        self.assertIn("user_alpha_one", usernames)
        self.assertIn("user_alpha_two", usernames)

    def test_messages_cross_account_data_does_not_appear(self):
        """2. Another account's messages/interactions do not appear."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/messages?account={ref}")
        data = response.get_json()
        rendered = str(data)
        self.assertNotIn("user_bravo_one", rendered)
        self.assertNotIn("bravo_account", rendered)

    def test_messages_filters_remain_account_scoped(self):
        """3. Filters remain account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for filter_value in ("all", "prompt", "messages", "followups", "smart_replies"):
            response = self.client.get(f"/app/api/messages?account={ref}&filter={filter_value}")
            self.assertEqual(response.status_code, 200)
            rendered = str(response.get_json())
            self.assertNotIn("user_bravo_one", rendered)
            self.assertNotIn("bravo_account", rendered)

    def test_messages_pagination_remains_account_scoped(self):
        """4. Pagination remains account-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page1 = self.client.get(f"/app/api/messages?account={ref}&page=1").get_json()
        self.assertEqual(page1["pagination"]["page"], 1)
        # No cross-account data on any page
        self.assertNotIn("user_bravo_one", str(page1))

    def test_messages_empty_state_works(self):
        """5. Empty state works."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        # Create a new empty account for owner A
        with self.Session.begin() as session:
            empty_account = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000099",
                username="empty_account",
                connection_status="connected",
            )
            session.add(empty_account)
            session.flush()
            session.add(InstagramCredential(
                instagram_account_id=empty_account.id,
                token_ciphertext="ciphertext-empty",
                encryption_key_id="KEY_2026_01",
                scopes=["instagram_business_basic"],
                token_status="valid",
            ))
            empty_ref = action_reference_for_account(
                self.owner_a.id,
                empty_account.id,
                "tenant-dashboard-select",
                secret_key=self.app.secret_key,
            )
        response = self.client.get(f"/app/messages?account={empty_ref}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No messages or interactions yet", response.get_data(as_text=True))

    def test_messages_invalid_account_fails_safely(self):
        """6. Invalid/stale selected account fails safely."""
        self._login_as(self.owner_a)
        response = self.client.get("/app/messages?account=malformed")
        self.assertEqual(response.status_code, 404)

    def test_messages_no_secrets_in_html(self):
        """7. No secrets/tokens/internal credentials appear in HTML."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/messages?account={ref}").get_data(as_text=True)
        for forbidden in (
            "ciphertext-a-secret",
            "KEY_2026_01",
            "10000000000000001",
            "recipient-a-secret",
            "comment-a-1-secret",
            "smart-reply-a-secret",
            "event-a-comment-secret",
            "followup-a-pending-secret",
            "private smart reply content a",
            "https://internal.example.invalid/a-followup",
        ):
            self.assertNotIn(forbidden, page)

    # ==================================================================
    # CONTENT TESTS
    # ==================================================================

    def test_content_selected_account_only_sees_own_content(self):
        """8. Selected account only sees its own content."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/content?account={ref}")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        public_ids = {item["public_id"] for item in data["content"]}
        self.assertIn("opaque-prompt-a1", public_ids)
        self.assertIn("opaque-prompt-a2", public_ids)

    def test_content_cross_account_never_appears(self):
        """9. Content from another account never appears."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        response = self.client.get(f"/app/api/content?account={ref}")
        rendered = str(response.get_json())
        self.assertNotIn("opaque-prompt-b1", rendered)
        self.assertNotIn("cross-owner-b", rendered)

    def test_content_prompt_posts_correctly_associated(self):
        """10. Prompt posts are correctly associated with their account."""
        self._login_as(self.owner_b)
        ref = self._reference(self.owner_b, self.account_b)
        response = self.client.get(f"/app/api/content?account={ref}")
        data = response.get_json()
        public_ids = {item["public_id"] for item in data["content"]}
        self.assertIn("opaque-prompt-b1", public_ids)
        self.assertNotIn("opaque-prompt-a1", public_ids)

    def test_content_viewer_links_use_opaque_public_ids(self):
        """11. Prompt viewer links use opaque public IDs."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('href="/p/o/opaque-prompt-a1"', page)
        self.assertIn('href="/p/o/opaque-prompt-a2"', page)
        # Internal media IDs must not appear
        self.assertNotIn("media-a-1-secret", page)
        self.assertNotIn("media-a-2-secret", page)

    def test_content_external_instagram_links_are_safe_https(self):
        """12. External Instagram links are safe HTTPS links."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('href="https://www.instagram.com/p/public-a1/"', page)

    def test_content_unsafe_urls_not_rendered(self):
        """13. Unsafe URLs are not rendered."""
        with self.Session.begin() as session:
            post = session.scalar(
                select(InstagramPromptPost).where(
                    InstagramPromptPost.public_id == "opaque-prompt-a2"
                )
            )
            post.permalink = "http://malicious.example/exploit"
            post.cover_url = "http://internal.local/image.jpg"
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertNotIn("malicious.example", page)
        self.assertNotIn("internal.local", page)
        self.assertNotIn("http://", page)

    def test_content_empty_state_works(self):
        """14. Empty state works."""
        self._login_as(self.owner_a)
        with self.Session.begin() as session:
            empty_account = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000088",
                username="empty_content",
                connection_status="connected",
            )
            session.add(empty_account)
            session.flush()
            session.add(InstagramCredential(
                instagram_account_id=empty_account.id,
                token_ciphertext="ciphertext-empty-c",
                encryption_key_id="KEY_2026_01",
                scopes=["instagram_business_basic"],
                token_status="valid",
            ))
            empty_ref = action_reference_for_account(
                self.owner_a.id,
                empty_account.id,
                "tenant-dashboard-select",
                secret_key=self.app.secret_key,
            )
        response = self.client.get(f"/app/content?account={empty_ref}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No prompt-enabled content yet", response.get_data(as_text=True))

    def test_content_draft_edit_route_updates_owned_item(self):
        """Draft edit succeeds only when the page-generated opaque reference is used under the selected account."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Original caption",
                    content_type="image",
                    media_url="https://cdn.example.com/original.jpg",
                )

        page_response = self.client.get(f"/app/content?account={ref}")
        self.assertEqual(page_response.status_code, 200)
        self.assertIn('name="draft_ref"', page_response.get_data(as_text=True))
        self.assertIn('name="csrf_token"', page_response.get_data(as_text=True))

        page_html = page_response.get_data(as_text=True)
        match = re.search(
            r'action="/app/content/drafts/edit\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        rendered_draft_ref = match.group(1)
        self.assertTrue(rendered_draft_ref)

        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', page_html)
        self.assertIsNotNone(csrf_match)
        rendered_csrf_token = csrf_match.group(1)
        self.assertTrue(rendered_csrf_token)

        response = self.client.post(
            f"/app/content/drafts/edit?account={ref}",
            data={
                "csrf_token": rendered_csrf_token,
                "draft_ref": rendered_draft_ref,
                "content_type": "video",
                "caption": "Updated caption",
                "media_url": "https://cdn.example.com/updated.mp4",
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/app/content?account=", response.location)

        with self.Session.begin() as session:
            updated = session.get(InstagramContentItem, draft.id)
            self.assertEqual(updated.caption, "Updated caption")
            self.assertEqual(updated.content_type, "video")
            self.assertEqual(updated.media_url, "https://cdn.example.com/updated.mp4")

    def test_content_draft_edit_route_rejects_cross_account_and_invalid_input(self):
        """Cross-account edits and invalid payloads are rejected without partial persistence."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)
        ref_b = self._reference(self.owner_b, self.account_b)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Locked caption",
                    content_type="image",
                    media_url="https://cdn.example.com/locked.jpg",
                )
                draft_ref = _content_draft_reference(
                    self.owner_a.id,
                    draft.id,
                    purpose="content-draft-edit",
                    now=None,
                )

        cross_account_response = self.client.post(
            f"/app/content/drafts/edit?account={ref_b}",
            data={
                "csrf_token": "csrf-test-token",
                "draft_ref": draft_ref,
                "content_type": "carousel",
                "caption": "Should not save",
                "media_url": "https://cdn.example.com/forbidden.jpg",
            },
            follow_redirects=False,
        )
        self.assertEqual(cross_account_response.status_code, 404)
        self.assertNotIn("Should not save", cross_account_response.get_data(as_text=True))

        invalid_response = self.client.post(
            f"/app/content/drafts/edit?account={ref_a}",
            data={
                "csrf_token": "csrf-test-token",
                "draft_ref": draft_ref,
                "content_type": "story",
                "caption": "",
                "media_url": "https://cdn.example.com/invalid.mp4",
            },
            follow_redirects=False,
        )
        self.assertEqual(invalid_response.status_code, 400)

        with self.Session.begin() as session:
            persisted = session.get(InstagramContentItem, draft.id)
            self.assertEqual(persisted.caption, "Locked caption")
            self.assertEqual(persisted.content_type, "image")
            self.assertEqual(persisted.media_url, "https://cdn.example.com/locked.jpg")

    def test_content_draft_status_route_transitions_owned_item(self):
        """Status changes use the page-generated reference for both supported transitions."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Status caption",
                    content_type="image",
                    media_url="https://cdn.example.com/status.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', page_html)
        status_ref_match = re.search(
            r'action="/app/content/drafts/status\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        )
        self.assertIsNotNone(csrf_match)
        self.assertIsNotNone(status_ref_match)
        csrf = csrf_match.group(1)
        status_ref = status_ref_match.group(1)

        ready_response = self.client.post(
            f"/app/content/drafts/status?account={ref_a}",
            data={"csrf_token": csrf, "draft_ref": status_ref, "status": "ready"},
            follow_redirects=False,
        )
        self.assertEqual(ready_response.status_code, 303)
        self.assertIn("/app/content?account=", ready_response.location)

        with self.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, draft.id).status, "ready")

        draft_page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        status_ref = re.search(
            r'action="/app/content/drafts/status\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            draft_page_html,
            re.DOTALL,
        ).group(1)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', draft_page_html).group(1)

        draft_response = self.client.post(
            f"/app/content/drafts/status?account={ref_a}",
            data={"csrf_token": csrf, "draft_ref": status_ref, "status": "draft"},
            follow_redirects=False,
        )
        self.assertEqual(draft_response.status_code, 303)

        with self.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, draft.id).status, "draft")

    def test_content_draft_status_route_rejects_invalid_or_cross_account_updates_without_mutation(self):
        """Invalid targets and a foreign account context leave the owned item unchanged."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)
        ref_b = self._reference(self.owner_b, self.account_b)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Protected status caption",
                    content_type="image",
                    media_url="https://cdn.example.com/protected-status.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page_html).group(1)
        status_ref = re.search(
            r'action="/app/content/drafts/status\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        ).group(1)

        invalid_response = self.client.post(
            f"/app/content/drafts/status?account={ref_a}",
            data={"csrf_token": csrf, "draft_ref": status_ref, "status": "published"},
            follow_redirects=False,
        )
        self.assertEqual(invalid_response.status_code, 400)

        cross_account_response = self.client.post(
            f"/app/content/drafts/status?account={ref_b}",
            data={"csrf_token": csrf, "draft_ref": status_ref, "status": "ready"},
            follow_redirects=False,
        )
        self.assertEqual(cross_account_response.status_code, 404)

        with self.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, draft.id).status, "draft")

    def test_content_draft_status_route_rejects_invalid_csrf_without_mutation(self):
        """A status form with a page-issued reference cannot bypass CSRF protection."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="CSRF protected status caption",
                    content_type="image",
                    media_url="https://cdn.example.com/csrf-status.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        status_ref = re.search(
            r'action="/app/content/drafts/status\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        ).group(1)

        response = self.client.post(
            f"/app/content/drafts/status?account={ref_a}",
            data={"csrf_token": "incorrect-csrf-token", "draft_ref": status_ref, "status": "ready"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400)

        with self.Session.begin() as session:
            self.assertEqual(session.get(InstagramContentItem, draft.id).status, "draft")

    def test_content_filters_remain_tenant_scoped(self):
        """15. Filters remain tenant-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for filter_value in ("all", "prompt_enabled"):
            response = self.client.get(f"/app/api/content?account={ref}&filter={filter_value}")
            self.assertEqual(response.status_code, 200)
            rendered = str(response.get_json())
            self.assertNotIn("opaque-prompt-b1", rendered)
            self.assertNotIn("cross-owner-b", rendered)

    def test_content_pagination_remains_tenant_scoped(self):
        """16. Pagination remains tenant-scoped."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page1 = self.client.get(f"/app/api/content?account={ref}&page=1").get_json()
        self.assertEqual(page1["pagination"]["page"], 1)
        self.assertNotIn("opaque-prompt-b1", str(page1))

    # ==================================================================
    # NAVIGATION TESTS
    # ==================================================================

    def test_messages_link_is_enabled_in_sidebar(self):
        """17. Messages link is enabled."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('href="/app/messages', page)
        self.assertNotIn(">Messages <small>Coming soon</small>", page)

    def test_content_link_is_enabled_in_sidebar(self):
        """18. Content link is enabled."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertIn('href="/app/content', page)
        self.assertNotIn(">Content <small>Coming soon</small>", page)

    def test_sidebar_scheduler_and_analytics_are_enabled(self):
        """19. Phase 5D replaces the remaining Scheduler/Analytics placeholders."""
        self._login_as(self.owner_a)
        page = self.client.get("/app").get_data(as_text=True)
        self.assertEqual(page.count("Coming soon"), 0)
        for label in ("Scheduler", "Analytics"):
            self.assertIn(f">{label}</a>", page)
            self.assertNotIn(f">{label} <small>Coming soon</small>", page)
        # Existing nav links still present
        self.assertIn('href="/app/leads', page)
        self.assertIn('href="/app/automations', page)
        self.assertIn('href="/app/accounts', page)

    # ==================================================================
    # REGRESSION — existing pages still work
    # ==================================================================

    def test_existing_dashboard_pages_still_work(self):
        """20-24. Automations, Leads, Activity, Follow-ups, Accounts still work."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for path in ("/app", f"/app/accounts?account={ref}", f"/app/leads?account={ref}",
                     f"/app/activity?account={ref}", f"/app/followups?account={ref}"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, f"Failed for {path}")

    def test_messages_active_navigation_marker(self):
        """Messages page shows active nav marker."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/messages?account={ref}").get_data(as_text=True)
        self.assertIn('aria-current="page">Messages', page)

    def test_content_active_navigation_marker(self):
        """Content page shows active nav marker."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('aria-current="page">Content', page)


if __name__ == "__main__":
    unittest.main()
