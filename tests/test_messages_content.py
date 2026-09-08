"""Phase 5C — Messages & Content dashboard tests.

Verifies account-scoped tenant isolation, filters, pagination, empty states,
and safe URL rendering for the Messages and Content dashboard screens.
"""

import re
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

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
from database.content_media import (
    ContentMediaAsset,
    ContentMediaProviderError,
    ContentMediaUnavailableError,
    ContentMediaValidationError,
)
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

    def _enable_internal_prompt_for(self, account):
        self.app.extensions[
            "phase3a_auth"
        ].legacy_instagram_user_id = account.instagram_user_id

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
        self.assertIn(
            "No message activity yet. Recorded comment interactions, messages, smart replies, and follow-ups will appear here.",
            response.get_data(as_text=True),
        )

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

    def test_content_requires_authentication(self):
        """The new upload surface must never become publicly accessible."""
        response = self.client.get("/app/content", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_content_selected_account_only_sees_own_content(self):
        """8. Selected account only sees its own content."""
        self._enable_internal_prompt_for(self.account_a)
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
        self._enable_internal_prompt_for(self.account_b)
        self._login_as(self.owner_b)
        ref = self._reference(self.owner_b, self.account_b)
        response = self.client.get(f"/app/api/content?account={ref}")
        data = response.get_json()
        public_ids = {item["public_id"] for item in data["content"]}
        self.assertIn("opaque-prompt-b1", public_ids)
        self.assertNotIn("opaque-prompt-a1", public_ids)

    def test_content_prompt_library_links_use_opaque_ids_and_separate_actions(self):
        """Prompt-library wording and action layout must stay accurate and distinct."""
        self._enable_internal_prompt_for(self.account_a)
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('href="/p/o/opaque-prompt-a1"', page)
        self.assertIn('href="/p/o/opaque-prompt-a2"', page)
        self.assertIn("Tracked Instagram posts", page)
        self.assertIn("Tracked post", page)
        self.assertIn("Prompts available: 2", page)
        self.assertIn("View prompt library", page)
        self.assertIn('class="content-row-actions tracked-post-actions"', page)
        self.assertNotIn("View prompt libraryView on Instagram", page)
        self.assertNotIn("Recorded interaction", page)
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
        self.assertIn("No tracked Instagram posts yet", response.get_data(as_text=True))

    def test_content_renders_image_only_workflow_and_safe_lifecycle_labels(self):
        """A publishing UI mutation cannot re-enable unsupported formats or expose provider errors."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            session.add_all([
                InstagramContentItem(
                    instagram_account_id=self.account_a.id,
                    status="draft",
                    content_type="image",
                    caption="Draft image",
                    media_url="https://cdn.example.com/draft.jpg",
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_a.id,
                    status="scheduled",
                    content_type="image",
                    caption="Scheduled image",
                    media_url="https://cdn.example.com/scheduled.jpg",
                    scheduled_for=now + timedelta(days=1),
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_a.id,
                    status="published",
                    content_type="image",
                    caption="Published image",
                    media_url="https://cdn.example.com/published.jpg",
                    published_at=now,
                    provider_media_id="safe-provider-media-id",
                ),
                InstagramContentItem(
                    instagram_account_id=self.account_a.id,
                    status="failed",
                    content_type="image",
                    caption="Failed image",
                    media_url="https://cdn.example.com/failed.jpg",
                    publish_error_code="provider_rejected",
                ),
            ])

        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)

        self.assertIn('name="content_type" value="image"', page)
        self.assertIn("Create image post", page)
        self.assertIn('type="file" name="image_file"', page)
        self.assertIn('accept="image/jpeg,.jpg,.jpeg"', page)
        self.assertIn("Use a public image URL instead", page)
        self.assertIn('name="submit_action" value="save"', page)
        self.assertIn('name="submit_action" value="schedule"', page)
        self.assertIn("Scheduling uses Africa/Casablanca time.", page)
        self.assertEqual(page.count("Africa/Casablanca"), 1)
        self.assertIn("Scheduled posts", page)
        self.assertIn("Drafts", page)
        self.assertIn("Scheduled", page)
        self.assertIn("Published", page)
        self.assertIn("Needs attention", page)
        self.assertIn("Instagram could not publish this image", page)
        for unsupported in ("video", "carousel", "reel"):
            self.assertNotIn(f'<option value="{unsupported}">', page)
        self.assertNotIn("provider_rejected", page)

    def test_content_upload_availability_is_clear_without_disabling_url_fallback(self):
        """Missing Cloudinary configuration must leave a real URL fallback and no fake upload promise."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        with patch("tenant_dashboard.content_media_is_configured", return_value=False):
            page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn("Image upload is temporarily unavailable", page)
        self.assertIn('type="file" name="image_file"', page)
        self.assertIn("disabled", page)
        self.assertIn('type="url" name="media_url"', page)

    def test_content_single_page_hides_pagination_and_multiple_pages_preserve_context(self):
        """Pagination must be absent when redundant and retain account/filter context when needed."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        single = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertNotIn('class="content-pagination"', single)

        with self.Session.begin() as session:
            for index in range(24):
                session.add(InstagramPromptPost(
                    instagram_account_id=self.account_a.id,
                    media_id=f"pagination-media-{index}",
                    public_id=f"pagination-public-{index}",
                    prompts=[{"text": "Prompt"}],
                ))
        multiple = self.client.get(
            f"/app/content?account={ref}&filter=prompt_enabled&page=1"
        ).get_data(as_text=True)
        self.assertIn('class="content-pagination"', multiple)
        self.assertRegex(multiple, rf'href="/app/content\?account=[^&"]+&amp;filter=prompt_enabled&amp;page=2"')

    def test_content_thumbnail_has_clean_localized_failure_fallback(self):
        """A stale provider cover must not leave the browser's broken-image presentation visible."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('data-thumbnail-preview', page)
        self.assertIn('data-thumbnail-fallback hidden', page)
        self.assertIn("Image unavailable", page)

    def test_content_polish_copy_renders_in_english_french_and_arabic(self):
        """Missing locale entries must be visible through the real rendered Content page."""
        expected = {
            "en": ("Upload an image", "Tracked Instagram posts", "Scheduled posts"),
            "fr": ("Importer une image", "Publications Instagram suivies", "Publications planifiées"),
            "ar": ("تحميل صورة", "منشورات Instagram المتتبعة", "المنشورات المجدولة"),
        }
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        for locale, phrases in expected.items():
            with self.subTest(locale=locale):
                with self.client.session_transaction() as session:
                    session["postenza_locale"] = locale
                page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
                for phrase in phrases:
                    self.assertIn(phrase, page)

    def test_content_upload_save_creates_only_an_owned_draft_with_storage_metadata(self):
        """Removing account binding or storage metadata from uploaded drafts breaks this behavior."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        asset = ContentMediaAsset(
            url="https://res.cloudinary.com/postenza/image/upload/owned.jpg",
            asset_id=f"postenza/content/{self.account_a.id.hex}/0123456789abcdef0123456789abcdef",
        )

        with patch("tenant_dashboard.upload_content_image", return_value=asset) as upload_image:
            response = self.client.post(
                f"/app/content/drafts/create?account={ref}",
                data={
                    "csrf_token": csrf,
                    "content_type": "image",
                    "caption": "First line\n\n#Postenza #Launch",
                    "submit_action": "save",
                    "image_file": (BytesIO(b"bounded-jpeg"), "launch.jpg", "image/jpeg"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(upload_image.call_args.kwargs["instagram_account_id"], self.account_a.id)
        with self.Session() as session:
            owned = session.scalars(select(InstagramContentItem).where(
                InstagramContentItem.instagram_account_id == self.account_a.id
            )).all()
            foreign = session.scalars(select(InstagramContentItem).where(
                InstagramContentItem.instagram_account_id == self.account_b.id
            )).all()
        self.assertEqual(len(owned), 1)
        self.assertEqual(foreign, [])
        self.assertEqual(owned[0].status, "draft")
        self.assertEqual(owned[0].caption, "First line\n\n#Postenza #Launch")
        self.assertEqual(owned[0].media_url, asset.url)
        self.assertEqual(owned[0].cover_url, asset.url)
        self.assertEqual(owned[0].metadata_json, {
            "content_type": "image",
            "storage": {"provider": "cloudinary", "asset_id": asset.asset_id},
        })

    def test_content_upload_schedule_creates_and_schedules_in_one_transaction(self):
        """A Schedule post submit must not leave an intermediate draft or bypass UTC scheduling."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        local_future = datetime.now(ZoneInfo("Africa/Casablanca")) + timedelta(days=2)
        asset = ContentMediaAsset(
            url="https://res.cloudinary.com/postenza/image/upload/scheduled.jpg",
            asset_id=f"postenza/content/{self.account_a.id.hex}/abcdef0123456789abcdef0123456789",
        )

        with patch("tenant_dashboard.upload_content_image", return_value=asset):
            response = self.client.post(
                f"/app/content/drafts/create?account={ref}",
                data={
                    "csrf_token": csrf,
                    "content_type": "image",
                    "caption": "Schedule from Content",
                    "submit_action": "schedule",
                    "date": local_future.strftime("%Y-%m-%d"),
                    "time": local_future.strftime("%H:%M"),
                    "image_file": (BytesIO(b"bounded-jpeg"), "scheduled.jpeg", "image/jpeg"),
                },
                content_type="multipart/form-data",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            item = session.scalar(select(InstagramContentItem).where(
                InstagramContentItem.instagram_account_id == self.account_a.id
            ))
        self.assertIsNotNone(item)
        self.assertEqual(item.status, "scheduled")
        expected = local_future.replace(second=0, microsecond=0).astimezone(timezone.utc)
        actual = item.scheduled_for.replace(tzinfo=timezone.utc)
        self.assertEqual(actual, expected)

    def test_content_create_preserves_direct_https_fallback_without_upload(self):
        """Removing the secondary URL workflow would break existing customers."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

        with patch("tenant_dashboard.upload_content_image", side_effect=AssertionError("upload used")):
            response = self.client.post(
                f"/app/content/drafts/create?account={ref}",
                data={
                    "csrf_token": csrf,
                    "content_type": "image",
                    "caption": "Existing URL flow",
                    "submit_action": "save",
                    "media_url": "https://images.example.com/fallback.jpg",
                },
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            item = session.scalar(select(InstagramContentItem).where(
                InstagramContentItem.instagram_account_id == self.account_a.id
            ))
        self.assertEqual(item.media_url, "https://images.example.com/fallback.jpg")
        self.assertEqual(item.metadata_json, {"content_type": "image"})

    def test_content_upload_errors_are_safe_and_do_not_create_rows(self):
        """Validation, configuration, and provider failures must remain user-facing and secret-free."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        cases = (
            (ContentMediaValidationError("image_too_large"), 400, "8 MB or smaller"),
            (ContentMediaValidationError("invalid_jpeg"), 400, "valid JPEG or JPG image"),
            (ContentMediaUnavailableError("upload_unavailable"), 503, "Image upload is temporarily unavailable"),
            (ContentMediaProviderError("upload_failed"), 502, "Image upload could not be completed"),
        )
        for error, expected_status, expected_copy in cases:
            with self.subTest(code=error.code), patch(
                "tenant_dashboard.upload_content_image", side_effect=error
            ):
                response = self.client.post(
                    f"/app/content/drafts/create?account={ref}",
                    data={
                        "csrf_token": csrf,
                        "caption": "Keep this caption",
                        "submit_action": "save",
                        "image_file": (BytesIO(b"bad"), "bad.jpg", "image/jpeg"),
                    },
                    content_type="multipart/form-data",
                )
                rendered = response.get_data(as_text=True)
                self.assertEqual(response.status_code, expected_status)
                self.assertIn(expected_copy, rendered)
                self.assertIn("Keep this caption", rendered)
                self.assertNotIn("CLOUDINARY_API_SECRET", rendered)
                self.assertNotIn("private-api-secret", rendered)
        with self.Session() as session:
            self.assertIsNone(session.scalar(select(InstagramContentItem)))

    def test_deleting_owned_uploaded_draft_removes_row_then_best_effort_asset(self):
        """A deletable uploaded draft should not leave its known Cloudinary asset behind."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        asset_id = f"postenza/content/{self.account_a.id.hex}/fedcba9876543210fedcba9876543210"
        with self.Session.begin() as session:
            draft = create_content_draft(
                session,
                instagram_account_id=self.account_a.id,
                caption="Uploaded draft",
                content_type="image",
                media_url="https://res.cloudinary.com/postenza/image/upload/delete.jpg",
                metadata_json={
                    "content_type": "image",
                    "storage": {"provider": "cloudinary", "asset_id": asset_id},
                },
            )
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        delete_ref = re.search(
            r'action="/app/content/drafts/delete\?account=[^"]+".*?name="draft_ref" value="([^"]+)"',
            page,
            re.DOTALL,
        ).group(1)

        with patch("tenant_dashboard.delete_content_image", return_value=False) as delete_asset:
            response = self.client.post(
                f"/app/content/drafts/delete?account={ref}",
                data={"csrf_token": csrf, "draft_ref": delete_ref},
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 303)
        delete_asset.assert_called_once_with(asset_id)
        with self.Session() as session:
            self.assertIsNone(session.get(InstagramContentItem, draft.id))

    def test_uploaded_asset_cleanup_is_attempted_when_database_creation_fails(self):
        """A database rollback after upload must trigger bounded best-effort cleanup."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        asset = ContentMediaAsset(
            url="https://res.cloudinary.com/postenza/image/upload/orphan.jpg",
            asset_id=f"postenza/content/{self.account_a.id.hex}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        with patch("tenant_dashboard.upload_content_image", return_value=asset), patch(
            "tenant_dashboard.create_content_draft", side_effect=ValueError("database rejected row")
        ), patch("tenant_dashboard.delete_content_image", return_value=False) as cleanup:
            response = self.client.post(
                f"/app/content/drafts/create?account={ref}",
                data={
                    "csrf_token": csrf,
                    "caption": "Rollback upload",
                    "submit_action": "save",
                    "image_file": (BytesIO(b"bounded-jpeg"), "rollback.jpg", "image/jpeg"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 400)
        cleanup.assert_called_once_with(asset.asset_id)
        with self.Session() as session:
            self.assertIsNone(session.scalar(select(InstagramContentItem)))

    def test_published_content_cannot_be_deleted_or_remove_its_stored_preview(self):
        """The unpublished-delete action must not remove published history or its media asset."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        asset_id = f"postenza/content/{self.account_a.id.hex}/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        with self.Session.begin() as session:
            item = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="published",
                content_type="image",
                caption="Published history",
                media_url="https://res.cloudinary.com/postenza/image/upload/history.jpg",
                metadata_json={"content_type": "image", "storage": {"provider": "cloudinary", "asset_id": asset_id}},
            )
            session.add(item)
            session.flush()
            item_id = item.id
        with self.app.app_context():
            delete_ref = _content_draft_reference(
                self.owner_a.id,
                item_id,
                purpose="content-draft-delete",
            )
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        with patch("tenant_dashboard.delete_content_image") as cleanup:
            response = self.client.post(
                f"/app/content/drafts/delete?account={ref}",
                data={"csrf_token": csrf, "draft_ref": delete_ref},
            )
        self.assertEqual(response.status_code, 400)
        cleanup.assert_not_called()
        with self.Session() as session:
            self.assertIsNotNone(session.get(InstagramContentItem, item_id))

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
        with self.app.app_context():
            rendered_draft_ref = _content_draft_reference(
                self.owner_a.id,
                draft.id,
                purpose="content-draft-edit",
            )
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

    def test_content_draft_edit_invalid_url_rerenders_form_without_mutation(self):
        """An invalid edit URL stays in the owned browser form for correction."""

        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Original edit caption",
                    content_type="image",
                    media_url="https://cdn.example.com/original-edit.jpg",
                )
                draft_ref = _content_draft_reference(
                    self.owner_a.id,
                    draft.id,
                    purpose="content-draft-edit",
                )

        response = self.client.post(
            f"/app/content/drafts/edit?account={ref}",
            data={
                "csrf_token": "csrf-test-token",
                "draft_ref": draft_ref,
                "content_type": "image",
                "caption": "Correct this edited caption",
                "media_url": "http://example.com/not-public.jpg",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "text/html; charset=utf-8")
        page = response.get_data(as_text=True)
        self.assertIn('class="alert alert-error" role="alert"', page)
        self.assertIn('value="http://example.com/not-public.jpg"', page)
        self.assertIn("Correct this edited caption", page)
        with self.Session() as session:
            persisted = session.get(InstagramContentItem, draft.id)
            self.assertEqual(persisted.caption, "Original edit caption")
            self.assertEqual(
                persisted.media_url,
                "https://cdn.example.com/original-edit.jpg",
            )

    def test_content_draft_recover_returns_owned_failed_item_to_draft_and_editable(self):
        """Recovering an owned failed item returns it to Draft with content intact."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            failed = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="failed",
                content_type="image",
                caption="Recover caption",
                media_url="https://cdn.example.com/recover.jpg",
                cover_url="https://cdn.example.com/recover-cover.jpg",
                alt_text="Recover alt",
                metadata_json={"content_type": "image", "keep": True},
                scheduled_for=now + timedelta(hours=1),
                publish_error_code="provider_rejected",
                provider_request_may_have_been_sent=True,
            )
            session.add(failed)
            session.flush()
            failed_id = failed.id

        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn("Needs attention", page)
        self.assertIn("Recover caption", page)
        self.assertIn('action="/app/content/drafts/recover?account=', page)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        recover_ref = re.search(
            r'action="/app/content/drafts/recover\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page,
            re.DOTALL,
        ).group(1)
        self.assertTrue(csrf)
        self.assertTrue(recover_ref)

        response = self.client.post(
            f"/app/content/drafts/recover?account={ref}",
            data={"csrf_token": csrf, "draft_ref": recover_ref},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/app/content?account=", response.location)

        with self.Session() as session:
            recovered = session.get(InstagramContentItem, failed_id)
            self.assertEqual(recovered.status, "draft")
            self.assertEqual(recovered.caption, "Recover caption")
            self.assertEqual(recovered.media_url, "https://cdn.example.com/recover.jpg")
            self.assertEqual(recovered.cover_url, "https://cdn.example.com/recover-cover.jpg")
            self.assertEqual(recovered.alt_text, "Recover alt")
            self.assertEqual(recovered.metadata_json, {"content_type": "image", "keep": True})
            # No scheduled/publishing lifecycle metadata lingers; no publish side effect.
            self.assertIsNone(recovered.scheduled_for)
            self.assertIsNone(recovered.publish_error_code)
            self.assertIsNone(recovered.published_at)
            self.assertIsNone(recovered.provider_media_id)
            self.assertFalse(recovered.provider_request_may_have_been_sent)

        # The recovered item is a Draft and editable through the existing Draft editor.
        recovered_page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn("Edit image draft", recovered_page)
        self.assertIn("Recover caption", recovered_page)
        self.assertNotIn('action="/app/content/drafts/recover', recovered_page)
    def test_failed_content_recovery_rejects_csrf_forged_ref_and_healthy_states(self):
        """CSRF, invalid references, and healthy states are rejected without mutation."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            failed = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="failed",
                content_type="image",
                caption="Recover locked",
                media_url="https://cdn.example.com/locked.jpg",
                scheduled_for=now + timedelta(hours=1),
                publish_error_code="provider_rejected",
            )
            ready = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="ready",
                content_type="image",
                caption="Ready locked",
                media_url="https://cdn.example.com/ready.jpg",
            )
            scheduled = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="scheduled",
                content_type="image",
                caption="Scheduled locked",
                media_url="https://cdn.example.com/scheduled.jpg",
                scheduled_for=now + timedelta(days=1),
            )
            published = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="published",
                content_type="image",
                caption="Published locked",
                media_url="https://cdn.example.com/published.jpg",
                published_at=now,
                provider_media_id="locked-provider-id",
            )
            session.add_all([failed, ready, scheduled, published])
            session.flush()
            failed_id = failed.id

        with self.app.app_context():
            recover_ref = _content_draft_reference(self.owner_a.id, failed.id, purpose="content-recover")
            wrong_purpose_ref = _content_draft_reference(self.owner_a.id, failed.id, purpose="content-draft-edit")
            ready_ref = _content_draft_reference(self.owner_a.id, ready.id, purpose="content-recover")
            scheduled_ref = _content_draft_reference(self.owner_a.id, scheduled.id, purpose="content-recover")
            published_ref = _content_draft_reference(self.owner_a.id, published.id, purpose="content-recover")

        # CSRF is required.
        no_csrf = self.client.post(
            f"/app/content/drafts/recover?account={ref_a}",
            data={"draft_ref": recover_ref},
            follow_redirects=False,
        )
        self.assertEqual(no_csrf.status_code, 400)

        # A forged / mis-purposed reference is rejected safely.
        forged = self.client.post(
            f"/app/content/drafts/recover?account={ref_a}",
            data={"csrf_token": "csrf-test-token", "draft_ref": wrong_purpose_ref},
            follow_redirects=False,
        )
        self.assertEqual(forged.status_code, 400)

        # Healthy scheduled/published/ready items cannot use this action.
        for item_ref in (ready_ref, scheduled_ref, published_ref):
            response = self.client.post(
                f"/app/content/drafts/recover?account={ref_a}",
                data={"csrf_token": "csrf-test-token", "draft_ref": item_ref},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 400, item_ref)

        with self.Session() as session:
            failed = session.get(InstagramContentItem, failed_id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.publish_error_code, "provider_rejected")
            for caption, expected_status in (
                ("Ready locked", "ready"),
                ("Scheduled locked", "scheduled"),
                ("Published locked", "published"),
            ):
                row = session.scalar(
                    select(InstagramContentItem).where(
                        InstagramContentItem.caption == caption
                    )
                )
                self.assertEqual(row.status, expected_status)
            published = session.scalar(
                select(InstagramContentItem).where(
                    InstagramContentItem.caption == "Published locked"
                )
            )
            self.assertEqual(published.provider_media_id, "locked-provider-id")

    def test_failed_content_recovery_is_tenant_isolated_and_account_scoped(self):
        """Another tenant and another account of the same owner cannot recover the item."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)
        with self.Session.begin() as session:
            failed = InstagramContentItem(
                instagram_account_id=self.account_a.id,
                status="failed",
                content_type="image",
                caption="Tenant locked",
                media_url="https://cdn.example.com/tenant-locked.jpg",
                publish_error_code="provider_rejected",
            )
            session.add(failed)
            second_account = InstagramAccount(
                user_id=self.owner_a.id,
                instagram_user_id="10000000000000003",
                username="alpha_second",
                connection_status="connected",
            )
            session.add(second_account)
            session.flush()
            failed_id = failed.id
            second_account_id = second_account.id

        with self.app.app_context():
            recover_ref = _content_draft_reference(self.owner_a.id, failed.id, purpose="content-recover")

        # Another account of the same owner cannot recover the item.
        wrong_account_ref = action_reference_for_account(
            self.owner_a.id,
            second_account_id,
            "tenant-dashboard-select",
            secret_key=self.app.secret_key,
        )
        wrong_account = self.client.post(
            f"/app/content/drafts/recover?account={wrong_account_ref}",
            data={"csrf_token": "csrf-test-token", "draft_ref": recover_ref},
            follow_redirects=False,
        )
        self.assertEqual(wrong_account.status_code, 400)

        # Another tenant cannot recover the item.
        self._login_as(self.owner_b)
        ref_b = self._reference(self.owner_b, self.account_b)
        cross_tenant = self.client.post(
            f"/app/content/drafts/recover?account={ref_b}",
            data={"csrf_token": "csrf-test-token", "draft_ref": recover_ref},
            follow_redirects=False,
        )
        self.assertEqual(cross_tenant.status_code, 400)

        with self.Session() as session:
            failed = session.get(InstagramContentItem, failed_id)
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.caption, "Tenant locked")
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

    def test_content_draft_delete_route_removes_owned_item_from_rendered_form(self):
        """Delete uses the page-issued token and selected-account context to remove its draft."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Delete this owned draft",
                    content_type="image",
                    media_url="https://cdn.example.com/delete-owned.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', page_html)
        delete_ref_match = re.search(
            r'action="/app/content/drafts/delete\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        )
        self.assertIsNotNone(csrf_match)
        self.assertIsNotNone(delete_ref_match)

        response = self.client.post(
            f"/app/content/drafts/delete?account={ref_a}",
            data={
                "csrf_token": csrf_match.group(1),
                "draft_ref": delete_ref_match.group(1),
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("/app/content?account=", response.location)

        with self.Session.begin() as session:
            self.assertIsNone(session.get(InstagramContentItem, draft.id))

    def test_content_draft_delete_route_rejects_invalid_or_cross_account_requests_without_mutation(self):
        """Invalid references and foreign account contexts cannot delete either account's draft."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)
        ref_b = self._reference(self.owner_b, self.account_b)

        with self.app.app_context():
            with self.Session.begin() as session:
                owned_draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="Keep owned draft",
                    content_type="image",
                    media_url="https://cdn.example.com/keep-owned.jpg",
                )
                foreign_draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_b.id,
                    caption="Keep foreign draft",
                    content_type="image",
                    media_url="https://cdn.example.com/keep-foreign.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page_html).group(1)
        delete_ref = re.search(
            r'action="/app/content/drafts/delete\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        ).group(1)

        invalid_ref_response = self.client.post(
            f"/app/content/drafts/delete?account={ref_a}",
            data={"csrf_token": csrf, "draft_ref": "invalid-delete-reference"},
            follow_redirects=False,
        )
        self.assertEqual(invalid_ref_response.status_code, 400)

        cross_account_response = self.client.post(
            f"/app/content/drafts/delete?account={ref_b}",
            data={"csrf_token": csrf, "draft_ref": delete_ref},
            follow_redirects=False,
        )
        self.assertEqual(cross_account_response.status_code, 404)

        with self.Session.begin() as session:
            self.assertIsNotNone(session.get(InstagramContentItem, owned_draft.id))
            self.assertIsNotNone(session.get(InstagramContentItem, foreign_draft.id))

    def test_content_draft_delete_route_rejects_invalid_csrf_without_mutation(self):
        """A valid delete reference alone cannot bypass CSRF protection."""
        self._login_as(self.owner_a)
        ref_a = self._reference(self.owner_a, self.account_a)

        with self.app.app_context():
            with self.Session.begin() as session:
                draft = create_content_draft(
                    session,
                    instagram_account_id=self.account_a.id,
                    caption="CSRF protected delete draft",
                    content_type="image",
                    media_url="https://cdn.example.com/csrf-delete.jpg",
                )

        page_html = self.client.get(f"/app/content?account={ref_a}").get_data(as_text=True)
        delete_ref = re.search(
            r'action="/app/content/drafts/delete\?account=[^"]+".*?'
            r'name="draft_ref" value="([^"]+)"',
            page_html,
            re.DOTALL,
        ).group(1)

        response = self.client.post(
            f"/app/content/drafts/delete?account={ref_a}",
            data={"csrf_token": "incorrect-csrf-token", "draft_ref": delete_ref},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400)

        with self.Session.begin() as session:
            self.assertIsNotNone(session.get(InstagramContentItem, draft.id))

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
        self.assertIn('href="/app/leads', page)
        self.assertNotIn(">Engagement <small>Coming soon</small>", page)

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
        for label in ("Calendar", "Overview"):
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
        self.assertIn('aria-current="page"><svg aria-hidden="true"', page)
        self.assertIn('</svg>Engagement</a>', page)

    def test_content_active_navigation_marker(self):
        """Content page shows active nav marker."""
        self._login_as(self.owner_a)
        ref = self._reference(self.owner_a, self.account_a)
        page = self.client.get(f"/app/content?account={ref}").get_data(as_text=True)
        self.assertIn('aria-current="page"><svg aria-hidden="true"', page)
        self.assertIn('</svg>Content</a>', page)


if __name__ == "__main__":
    unittest.main()
