"""Focused public Postenza landing-page route and template tests."""

import importlib.util
import os
import sys
import tempfile
import unittest
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
        with patch.dict(os.environ, environment, clear=False), patch("threading.Thread", _NoopThread):
            spec.loader.exec_module(module)
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
        self.assertIn("Put your Instagram on autopilot.", page)
        self.assertIn("Postenza", page)
        self.assertIn('href="/register"', page)
        self.assertIn('href="/login"', page)

    def test_health_is_a_safe_liveness_response(self):
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})

    def test_landing_uses_existing_session_locale_and_rtl(self):
        page = self._page_for_locale("ar")

        self.assertIn('lang="ar" dir="rtl"', page)
        self.assertIn("Postenza", page)
        self.assertIn("ضع إنستغرام الخاص بك على الطيار الآلي.", page)

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

        for text in ("Schedule content", "Automate replies", "Private follow-ups", "Smart replies"):
            self.assertIn(text, page)
        self.assertNotIn("TikTok", page)
        self.assertNotIn("Sociora", page)
        self.assertIn("Image publishing is supported today.", page)

    def test_landing_has_accessible_mobile_navigation_and_seo_metadata(self):
        page = self.client.get("/").get_data(as_text=True)

        self.assertIn('aria-controls="landing-navigation"', page)
        self.assertIn('aria-expanded="false"', page)
        self.assertIn('name="description"', page)
        self.assertIn('property="og:title"', page)
        self.assertIn('rel="canonical"', page)

    def test_landing_localizes_french_without_translating_product_name(self):
        page = self._page_for_locale("fr")

        self.assertIn('lang="fr" dir="ltr"', page)
        self.assertIn("Mettez votre Instagram en pilote automatique.", page)
        self.assertIn("Postenza", page)


if __name__ == "__main__":
    unittest.main()
