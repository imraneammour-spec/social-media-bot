"""Product semantics and localization exercised through the real tenant routes."""
import unittest
import re
from pathlib import Path
from string import Formatter
import shutil
import subprocess
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from database.models import InstagramAutomationConfig, InstagramOAuthDirectMessageInteraction, InstagramOAuthFollowupJob
from tests import test_tenant_dashboard as fixture


class ProductPolishTest(unittest.TestCase):
    def setUp(self):
        fixture.TenantDashboardTest.setUp(self)
        with self.Session.begin() as session:
            session.add(InstagramAutomationConfig(instagram_account_id=self.account_a1.id,
                        delivery_mode="direct_message", followup_enabled=True,
                        followup_message_text="Configured follow-up", trigger_keywords=["directtest"]))
    tearDown = fixture.TenantDashboardTest.tearDown
    _seed = fixture.TenantDashboardTest._seed

    def get(self, path):
        from auth import action_reference_for_account
        reference = action_reference_for_account(self.owner_a.id, self.account_a1.id,
                                                "tenant-dashboard-select", secret_key=self.app.secret_key)
        return self.client.get(path, query_string={"account": reference})

    def login(self, locale="en", legacy=False):
        from auth import USER_SESSION_KEY
        with self.client.session_transaction() as session:
            session.clear()
            session[USER_SESSION_KEY] = str(self.owner_a.id)
            session["sociora_locale" if legacy else "postenza_locale"] = locale

    def test_direct_automation_explains_response_trigger_and_preserves_storage_values(self):
        self.login()
        page = self.get("/app/automations").get_data(as_text=True)
        self.assertIn("Delay after recipient reply", page)
        self.assertIn("If the recipient never replies, no follow-up is sent.", page)
        self.assertNotIn('value="prompt_request"', page)
        self.assertIn('value="direct_message"', page)
        self.assertNotIn("Legacy prompt delivery", page)

    def test_navigation_and_locale_session_fallback(self):
        for locale, label, direction in (("en", "Analytics", "ltr"), ("fr", "Statistiques", "ltr"), ("ar", "التحليلات", "rtl")):
            self.login(locale, legacy=True)
            response = self.get("/app/analytics")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn(f'dir="{direction}"', page)
            self.assertIn(label, page)
            self.assertIn('data-nav="analytics" aria-current="page"', page)
            if locale != "en":
                self.assertNotIn("Operational analytics for the selected account", page)

    def test_followups_present_waiting_and_expiry_without_sensitive_fields(self):
        self.login()
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            session.add(InstagramOAuthDirectMessageInteraction(
                instagram_account_id=self.account_a1.id, source_comment_id="secret-source",
                recipient_id="secret-recipient", initial_private_reply_at=now,
            ))
            session.add(InstagramOAuthFollowupJob(
                instagram_account_id=self.account_a1.id, source_message_id="secret-reply",
                recipient_id="secret-recipient-expired", message_text="secret-message",
                run_at=now-timedelta(days=1), state="failed", last_error_code="outside_provider_window",
                provider_window_expires_at=now-timedelta(hours=1), failed_at=now,
            ))
        page = self.get("/app/followups").get_data(as_text=True)
        self.assertIn("Waiting for recipient reply", page)
        self.assertIn("Expired", page)
        for private in ("secret-source", "secret-recipient", "secret-reply", "secret-message", str(self.account_a1.id)):
            self.assertNotIn(private, page)

    def test_calendar_does_not_duplicate_followup_management(self):
        self.login()
        page = self.get("/app/scheduler").get_data(as_text=True)
        self.assertIn("/app/followups", page)
        self.assertNotIn("Cancel follow-up", page)

    def test_calendar_does_not_load_followup_jobs(self):
        import tenant_dashboard as routes
        self.login()
        with patch.object(routes.tenant_data, "scheduler_jobs", side_effect=AssertionError("Unrelated job query")):
            self.assertEqual(self.get("/app/scheduler").status_code, 200)

    def test_image_link_validation_is_local_and_https_only(self):
        from dashboard.presentation import public_image_url
        self.assertEqual(public_image_url(" https://images.example.com/photo.jpg?version=1 "),
                         "https://images.example.com/photo.jpg?version=1")
        for value in ("http://example.com/a.jpg", "https://user:password@example.com/a.jpg",
                      "https://localhost/a.jpg", "https://127.0.0.1/a.jpg",
                      "https://[::1]/a.jpg", "https://192.168.1.2/a.jpg",
                      "https://example.com\\@localhost/a.jpg", "file:///photo.jpg", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    public_image_url(value)

    def test_timestamps_are_explicit_casablanca_and_no_english_months(self):
        from dashboard.presentation import display_time
        self.assertEqual(display_time("2026-09-05T12:00:00+00:00", "fr"), "05/09/2026 13:00")
        self.assertEqual(display_time("2026-09-05T12:00:00", "en"), "2026-09-05 13:00")

    def test_leads_fail_safely_when_direct_history_unavailable(self):
        import tenant_dashboard as routes
        self.login()
        real_query = routes._query
        def query(selected, reader, **kwargs):
            if reader is routes.direct_interactions:
                return routes._DATABASE_UNAVAILABLE
            return real_query(selected, reader, **kwargs)
        with patch.object(routes, "_query", side_effect=query):
            self.assertEqual(self.get("/app/leads").status_code, 503)

    def test_direct_metrics_are_account_scoped_and_use_reply_time(self):
        from database.tenant_insights import direct_metrics, direct_interactions
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            session.add(InstagramOAuthDirectMessageInteraction(
                instagram_account_id=self.account_a1.id, source_comment_id="old-comment",
                recipient_id="one-recipient", initial_private_reply_at=now-timedelta(days=4),
                recipient_reply_at=now))
            session.add(InstagramOAuthDirectMessageInteraction(
                instagram_account_id=self.account_b.id, source_comment_id="foreign-comment",
                recipient_id="foreign-recipient", initial_private_reply_at=now))
        with self.Session() as session:
            metrics = direct_metrics(session, instagram_account_id=self.account_a1.id, since=now-timedelta(days=1))
            self.assertEqual(metrics, {"direct_private_replies": 0, "qualifying_recipient_replies": 1})
            rows = direct_interactions(session, instagram_account_id=self.account_a1.id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0]), {"source_type", "status", "created_at", "replied_at"})

    def test_all_app_template_messages_have_complete_catalogs(self):
        from jinja2 import Environment, nodes
        from dashboard.i18n import TRANSLATIONS
        from dashboard.app_translations import APP_TRANSLATIONS, _MESSAGES
        self.assertEqual(len(_MESSAGES), len(APP_TRANSLATIONS["en"]), "Duplicate message IDs")
        root = Path(__file__).resolve().parents[1] / "dashboard/templates"
        keys = set()
        for path in [
            *root.glob("app_*.html"),
            root / "_engagement_tabs.html",
            root / "onboarding.html",
            root / "login.html",
            root / "register.html",
        ]:
            for call in Environment().parse(path.read_text(encoding="utf-8")).find_all(nodes.Call):
                if isinstance(call.node, nodes.Name) and call.node.name == "t" and call.args and isinstance(call.args[0], nodes.Const):
                    keys.add(call.args[0].value)
        for locale in ("en", "fr", "ar"):
            self.assertFalse(keys - APP_TRANSLATIONS[locale].keys() - TRANSLATIONS[locale].keys(), locale)
            for key, value in APP_TRANSLATIONS[locale].items():
                fields = lambda text: {field for _, field, _, _ in Formatter().parse(text) if field}
                self.assertTrue(value.strip(), key)
                self.assertEqual(fields(key), fields(value), (locale, key))

    def test_every_app_page_renders_in_all_locales_with_known_dynamic_labels(self):
        import tenant_dashboard as routes
        from dashboard.i18n import TRANSLATIONS, translate
        from dashboard.app_translations import APP_TRANSLATIONS
        for locale in ("en", "fr", "ar"):
            self.login(locale)
            seen = set()
            def checked(code, key, **values):
                seen.add(str(key or ""))
                return translate(code, key, **values)
            with patch.object(routes, "translate", side_effect=checked):
                for path in ("/app", "/app/accounts", "/app/automations", "/app/leads", "/app/messages",
                             "/app/activity", "/app/followups", "/app/analytics", "/app/content", "/app/scheduler"):
                    response = self.get(path)
                    self.assertEqual(response.status_code, 200, (locale, path))
                    self.assertNotIn("Sociora", response.get_data(as_text=True))
            # Date group headings are numeric, not translatable messages.
            missing = {key for key in seen if key and not key[:1].isdigit()} - APP_TRANSLATIONS[locale].keys() - TRANSLATIONS[locale].keys()
            self.assertFalse(missing, (locale, sorted(missing)))

    def test_analytics_range_action_uses_the_primary_button_component(self):
        self.login()
        page = self.get("/app/analytics").get_data(as_text=True)
        self.assertIn('<button class="button button-primary" type="submit">Apply range</button>', page)

    def test_image_form_rejects_invalid_url_without_creating_content(self):
        from auth import action_reference_for_account
        from database.models import InstagramContentItem
        from sqlalchemy import select
        self.login("fr")
        page = self.get("/app/content").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        account = action_reference_for_account(self.owner_a.id, self.account_a1.id,
                                              "tenant-dashboard-select", secret_key=self.app.secret_key)
        response = self.client.post("/app/content/drafts/create", query_string={"account": account},
                                    data={"csrf_token": csrf, "content_type": "image", "caption": "My caption",
                                          "media_url": "http://example.com/a.jpg"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("could not", response.get_data(as_text=True))
        with self.Session() as session:
            self.assertIsNone(session.scalar(select(InstagramContentItem).where(
                InstagramContentItem.instagram_account_id == self.account_a1.id)))

    def test_new_locale_preference_wins_without_changing_user_session(self):
        from auth import USER_SESSION_KEY
        self.login("ar", legacy=True)
        with self.client.session_transaction() as session:
            session["postenza_locale"] = "fr"
        self.assertIn('lang="fr" dir="ltr"', self.get("/app").get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertEqual(session[USER_SESSION_KEY], str(self.owner_a.id))
            self.assertEqual(session["sociora_locale"], "ar")

    def test_signed_in_connection_screen_and_errors_follow_locale(self):
        self.login("ar", legacy=True)
        page = self.client.get("/onboarding").get_data(as_text=True)
        self.assertIn('lang="ar" dir="rtl"', page)
        self.assertNotIn("Instagram workspace status", page)
        response = self.client.post("/connect/instagram")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Invalid form token", response.get_data(as_text=True))

    def test_followup_states_are_allowlisted_and_only_pending_is_cancellable(self):
        self.login()
        pending_before = self.get("/app/followups").get_data(as_text=True).count('name="scheduler_action"')
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            for state in ("pending", "processing", "sent", "failed", "cancelled"):
                session.add(InstagramOAuthFollowupJob(
                    instagram_account_id=self.account_a1.id, source_message_id="private-source-"+state,
                    recipient_id="private-recipient-"+state, message_text="private-body", state=state,
                    run_at=now, last_error_code="untrusted error credential=secret"))
        page = self.get("/app/followups").get_data(as_text=True)
        for state in ("Scheduled", "Processing", "Sent", "Failed", "Cancelled"):
            self.assertIn(state, page)
        self.assertEqual(page.count('name="scheduler_action"'), pending_before + 1)
        for private in ("private-source-", "private-recipient-", "private-body", "untrusted error", "credential=secret"):
            self.assertNotIn(private, page)

    def test_rendered_scripts_compile_in_every_locale(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is not installed")
        for locale in ("en", "fr", "ar"):
            self.login(locale)
            for path in ("/app", "/app/content", "/app/automations"):
                page = self.get(path).get_data(as_text=True)
                for script in re.findall(r"<script>([\s\S]*?)</script>", page):
                    result = subprocess.run([node, "--check"], input=script, text=True,
                                            encoding="utf-8", capture_output=True)
                    self.assertEqual(result.returncode, 0, (locale, path, result.stderr))

    def test_followup_can_be_disabled_from_rendered_form(self):
        from werkzeug.datastructures import MultiDict
        from sqlalchemy import select
        self.login()
        response = self.get("/app/automations")
        page = response.get_data(as_text=True)
        self.assertIn('type="hidden" name="message_followup_enabled" value="off"', page)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post("/app/automations", data=MultiDict([
            ("csrf_token", csrf), ("delivery_mode", "direct_message"),
            ("message_followup_enabled", "off"),
            ("message_trigger_keywords", "directtest"),
            ("message_followup_delay_minutes", "5")]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("Automation settings saved.", response.get_data(as_text=True))
        with self.Session() as session:
            config = session.scalar(select(InstagramAutomationConfig).where(
                InstagramAutomationConfig.instagram_account_id == self.account_a1.id))
            self.assertFalse(config.followup_enabled)
            self.assertEqual(config.followup_message_text, "Configured follow-up")
