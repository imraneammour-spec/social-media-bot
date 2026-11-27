"""Focused public Postenza landing-page route and template tests."""

import importlib.util
import io
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "webhook.py"


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


class LandingPageTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module_name = "postenza_landing_page_test_app"
        self.module = self._load_webhook_module()
        self.client = self.module.app.test_client()

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _load_webhook_module(self):
        environment = {
            "DATABASE_URL": f"sqlite:///{Path(self.temp_dir.name) / 'landing.db'}",
            "FLASK_SECRET_KEY": "landing-page-test-secret",
            "VERIFY_TOKEN": "landing-page-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
        }
        spec = importlib.util.spec_from_file_location(self.module_name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = module
        startup_output = io.StringIO()
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("threading.Thread", _NoopThread),
            redirect_stdout(startup_output),
        ):
            sys.modules.pop("legacy_bot", None)  # fresh DATA_DIR
            spec.loader.exec_module(module)
        self.startup_output = startup_output.getvalue()
        return module

    def _csrf_for_root(self):
        self.client.get("/")
        with self.client.session_transaction() as browser_session:
            return browser_session["_csrf_token"]

    def _page_for_locale(self, locale):
        response = self.client.post(
            "/locale",
            data={"locale": locale, "csrf_token": self._csrf_for_root()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_root_renders_the_postenza_landing_page(self):
        response = self.client.get("/")
        page = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Your Instagram replies itself.", page)
        self.assertIn("Postenza", page)
        self.assertIn('href="/register"', page)
        self.assertIn('href="/login"', page)

    def test_landing_header_keeps_official_logo_without_duplicate_hero_tagline(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn(
            '<a class="site-brand" href="#top"><img src="/static/brand/postenza-logo-light.png" alt="Postenza"></a>',
            page,
        )
        self.assertEqual(page.count("Social media, on autopilot."), 1)
        hero = page.split('<div class="hero-copy">', 1)[1].split(
            '<div class="product-stage"', 1
        )[0]
        self.assertNotIn("Social media, on autopilot.", hero)

    def test_landing_footer_legal_column_is_not_labelled_faq(self):
        page = self.client.get("/").get_data(as_text=True)
        footer = page.split("<footer", 1)[1]
        self.assertIn("<b>Legal</b>", footer)
        self.assertNotIn("<b>FAQ</b>", footer)

    def test_landing_tagline_is_localized_per_locale(self):
        expected = {
            "en": "Social media, on autopilot.",
            "fr": "Les réseaux sociaux, en pilote automatique.",
            "ar": "وسائل التواصل الاجتماعي على الطيار الآلي.",
        }
        for locale, tagline in expected.items():
            with self.subTest(locale=locale):
                page = self._page_for_locale(locale)
                self.assertEqual(page.count(tagline), 1)
                for other in expected.values():
                    if other != tagline:
                        self.assertNotIn(other, page)

    def test_landing_pages_do_not_mix_languages(self):
        en = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("إنستغرام ديالك", en)
        self.assertNotIn("كيجاوب", en)
        self.assertNotIn("reply-line", en)
        fr = self._page_for_locale("fr")
        self.assertNotIn("Your Instagram replies itself.", fr)
        self.assertNotIn("إنستغرام ديالك", fr)
        self.assertIn("Votre Instagram répond tout seul.", fr)
        ar = self._page_for_locale("ar")
        self.assertNotIn("Your Instagram replies itself.", ar)
        self.assertNotIn("Votre Instagram", ar)
        self.assertIn("إنستغرام يردّ بنفسه.", ar)

    def test_landing_and_pricing_have_no_competitor_comparison(self):
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale):
                page = self._page_for_locale(locale)
                self.assertNotIn("ManyChat", page)
                self.assertNotIn("manychat", page)
                self.assertNotIn('id="compare"', page)
        pricing = self.client.get("/pricing").get_data(as_text=True)
        self.assertNotIn("ManyChat", pricing)
        self.assertNotIn("manychat", pricing)

    def test_landing_hero_mock_is_a_labelled_example_without_an_ai_content_assistant_card(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Example workspace with sample data.", page)
        self.assertIn("Top post", page)
        self.assertIn("248 likes", page)
        self.assertNotIn("AI content assistant", page)
        self.assertNotIn("content assistant", page.lower())

    def test_unknown_pages_get_the_branded_postenza_404(self):
        for path in ("/no-such-page", "/app/no-such-page"):
            with self.subTest(path=path):
                response = self.client.get(path)
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 404)
                self.assertIn("Page not found", page)
                self.assertIn("Back to Postenza", page)
                self.assertIn('href="/"', page)
                self.assertIn("postenza-logo-light.png", page)

    def test_app_onboarding_redirects_to_onboarding(self):
        response = self.client.get("/app/onboarding", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/onboarding")

    def test_health_is_a_safe_liveness_response(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_startup_identifies_postenza_and_treats_legacy_token_as_optional(self):
        self.assertIn("Postenza", self.startup_output)
        self.assertIn("Optional legacy Instagram integration: not configured", self.startup_output)
        self.assertNotIn("Imran Social Media Bot", self.startup_output)
        self.assertNotIn("ERROR: INSTAGRAM_ACCESS_TOKEN missing", self.startup_output)

    def test_public_legal_pages_use_postenza_branding_and_readable_shell(self):
        for path in ("/privacy", "/terms", "/deletion"):
            with self.subTest(path=path):
                response = self.client.get(path)
                page = response.get_data(as_text=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Postenza", page)
                self.assertNotIn("Imran Social Media Bot", page)
                self.assertIn('href="/static/legal.css"', page)
                self.assertIn('href="/static/brand/postenza-favicon-32.png"', page)
                self.assertIn('href="/"', page)
                self.assertIn('<a class="skip-link" href="#main-content">Skip to content</a>', page)
                self.assertIn('<main id="main-content" tabindex="-1">', page)
                response.close()

    def test_landing_uses_existing_session_locale_and_rtl(self):
        page = self._page_for_locale("ar")

        self.assertIn('lang="ar" dir="rtl"', page)
        self.assertIn("Postenza", page)
        self.assertIn("إنستغرام يردّ بنفسه.", page)

    def test_public_locale_handler_rejects_bad_csrf_and_normalizes_unknown_locales(self):
        rejected = self.client.post("/locale", data={"locale": "ar", "csrf_token": "bad"})
        self.assertEqual(rejected.status_code, 400)

        page = self._page_for_locale("unsupported").lower()
        self.assertIn('lang="en" dir="ltr"', page)

    def test_existing_readiness_endpoint_remains_available(self):
        response = self.client.get("/api/v01-health")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Authentication is required."})

    def test_landing_includes_real_capabilities_and_safe_ctas(self):
        page = self.client.get("/").get_data(as_text=True)

        for text in ("Schedule content", "Automate replies", "Private follow-ups", "Ready replies"):
            self.assertIn(text, page)
        self.assertNotIn("TikTok", page)
        self.assertNotIn("Sociora", page)
        self.assertIn("Reels are supported.", page)
        self.assertIn("Image posts and Reels", page)

    def test_landing_has_accessible_mobile_navigation_and_seo_metadata(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('<a class="skip-link" href="#main-content">', page)
        self.assertIn('<main id="main-content">', page)
        self.assertIn('aria-controls="landing-navigation"', page)
        self.assertIn('aria-expanded="false"', page)
        self.assertIn('data-open-label="Close navigation"', page)
        self.assertIn('data-closed-label="Open navigation"', page)
        self.assertIn('name="description"', page)
        self.assertIn('property="og:title"', page)
        self.assertIn('rel="canonical"', page)

    def test_landing_mockup_status_is_localized(self):
        expected = {
            "en": "On",
            "fr": "Activée",
            "ar": "مفعّلة",
        }
        for locale, label in expected.items():
            with self.subTest(locale=locale):
                page = self._page_for_locale(locale)
                self.assertIn(f"<b>{label}</b>", page)

    def test_landing_metadata_uses_configured_https_origin_behind_proxy(self):
        with patch.object(
            self.module,
            "PUBLIC_BASE_URL",
            "https://postenza.app",
        ):
            page = self.client.get(
                "/",
                base_url="http://postenza.app",
            ).get_data(as_text=True)

        self.assertIn(
            '<link rel="canonical" href="https://postenza.app/">',
            page,
        )
        self.assertIn(
            '<meta property="og:image" content="https://postenza.app/static/brand/postenza-logo-light.png">',
            page,
        )
        self.assertNotIn('content="http://postenza.app/', page)

    def test_landing_metadata_rejects_untrusted_public_origin_and_keeps_local_fallback(self):
        with patch.object(
            self.module,
            "PUBLIC_BASE_URL",
            "https://postenza.app.evil.example",
        ):
            page = self.client.get(
                "/",
                base_url="http://localhost",
            ).get_data(as_text=True)

        self.assertIn('<link rel="canonical" href="http://localhost/">', page)
        self.assertIn(
            '<meta property="og:image" content="http://localhost/static/brand/postenza-logo-light.png">',
            page,
        )
        self.assertNotIn("postenza.app.evil.example", page)

    def test_landing_contains_google_site_verification_tag_once(self):
        page = self.client.get("/").get_data(as_text=True)
        verification_tag = (
            '<meta name="google-site-verification" '
            'content="DLtPOJlpIzjjlfxZ45fOISi0hD3JhPcp9L4D3f6LD4I" />'
        )

        self.assertEqual(page.count(verification_tag), 1)

    def test_sitemap_exposes_all_canonical_public_pages(self):
        response = self.client.get("/sitemap.xml")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/xml")
        root = ET.fromstring(response.get_data(as_text=True))
        locations = [
            element.text
            for element in root.findall("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        ]
        self.assertEqual(
            locations,
            [
                "https://postenza.app/",
                "https://postenza.app/features/instagram-scheduling",
                "https://postenza.app/features/instagram-automation",
                "https://postenza.app/pricing",
                "https://postenza.app/privacy",
                "https://postenza.app/terms",
                "https://postenza.app/deletion",
            ],
        )

    def test_robots_allows_the_homepage_and_protects_private_routes(self):
        response = self.client.get("/robots.txt")
        expected = (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /app\n"
            "Disallow: /dashboard\n"
            "Disallow: /login\n"
            "Disallow: /register\n"
            "Disallow: /logout\n"
            "Disallow: /onboarding\n"
            "Disallow: /auth/\n"
            "Disallow: /connect/\n"
            "Disallow: /disconnect/\n"
            "Disallow: /oauth/\n"
            "Disallow: /webhook\n"
            "Disallow: /billing/\n"
            "Disallow: /api/\n"
            "Disallow: /health\n"
            "Disallow: /ready\n"
            "Disallow: /p/\n"
            "\n"
            "Sitemap: https://postenza.app/sitemap.xml\n"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/plain")
        self.assertEqual(response.get_data(as_text=True), expected)
        self.assertNotIn("Disallow: /", response.get_data(as_text=True).splitlines())

    def test_landing_localizes_french_without_translating_product_name(self):
        page = self._page_for_locale("fr")

        self.assertIn('lang="fr" dir="ltr"', page)
        self.assertIn("Votre Instagram répond tout seul.", page)
        self.assertIn("Postenza", page)

    def test_landing_describes_message_activity_without_claiming_an_inbox(self):
        expected = {
            "en": "manage message activity",
            "fr": "suivez l’activité des messages",
            "ar": "تابع نشاط الرسائل",
        }
        for locale, wording in expected.items():
            with self.subTest(locale=locale):
                page = self._page_for_locale(locale)
                self.assertIn(wording, page)
                self.assertNotIn("conversation", page.lower())
                self.assertNotIn("المحادثات", page)


if __name__ == "__main__":
    unittest.main()
