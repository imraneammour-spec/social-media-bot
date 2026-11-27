"""Every user-visible string ships in English, French and Arabic.

Covers the public pages (landing, pricing, feature pages, auth, 404),
the billing page (plans + credit packs + allowance alerts), the tenant
dashboard (overview, content incl. Reel composer, scheduler, automations,
engagement), and a static completeness check: every literal ``t()`` key
used by a template must resolve in all three locales instead of falling
back to the English key.
"""

import importlib.util
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.i18n import LANDING_TRANSLATIONS, TRANSLATIONS, translate  # noqa: E402

HERO = {
    "en": "Your Instagram replies itself.",
    "fr": "Votre Instagram répond tout seul.",
    "ar": "إنستغرام يردّ بنفسه.",
}
PRICING_HEADING = {
    "en": "A plan for your Instagram workflow",
    "fr": "Une offre pour votre flux Instagram",
    "ar": "خطة لسير عملك على إنستغرام",
}
BILLING_HEADING = {
    "en": "Billing &amp; plans",
    "fr": "Facturation et offres",
    "ar": "الفوترة والخطط",
}
BUY_500 = {
    "en": "Buy 500 credits",
    "fr": "Acheter 500 crédits",
    "ar": "شراء 500 وحدة رصيد",
}
OVERVIEW_HEADING = {
    "en": "Overview",
    "fr": "Vue d’ensemble",
    "ar": "نظرة عامة",
}


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


def _set_locale(client, locale):
    with client.session_transaction() as session:
        session["postenza_locale"] = locale


class PublicPagesI18nTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.module_name = "postenza_i18n_public_test_app"
        self.module = self._load_webhook_module()
        self.client = self.module.app.test_client()

    def tearDown(self):
        sys.modules.pop(self.module_name, None)
        sys.modules.pop("legacy_bot", None)
        self.temp_dir.cleanup()

    def _load_webhook_module(self):
        environment = {
            "DATABASE_URL": f"sqlite:///{Path(self.temp_dir.name) / 'i18n.db'}",
            "FLASK_SECRET_KEY": "i18n-public-test-secret",
            "VERIFY_TOKEN": "i18n-verify-token",
            "META_APP_SECRET": "",
            "APP_ENV": "test",
            "ALLOW_UNSIGNED_WEBHOOKS": "1",
            "INSTAGRAM_ACCESS_TOKEN": "",
            "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
            "GEMINI_API_KEY": "",
        }
        spec = importlib.util.spec_from_file_location(self.module_name, ROOT / "webhook.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = module
        with (
            patch.dict(os.environ, environment, clear=False),
            patch("threading.Thread", _NoopThread),
            redirect_stdout(io.StringIO()),
        ):
            sys.modules.pop("legacy_bot", None)
            spec.loader.exec_module(module)
        return module

    def _page(self, path, locale):
        _set_locale(self.client, locale)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_landing_hero_and_pricing_in_all_locales_without_mixing(self):
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale):
                landing = self._page("/", locale)
                self.assertIn(f"<h1>{HERO[locale]}</h1>", landing)
                for other in HERO.values():
                    if other != HERO[locale]:
                        self.assertNotIn(other, landing)
                self.assertIn('name="locale"', landing)
                pricing = self._page("/pricing", locale)
                self.assertIn(PRICING_HEADING[locale], pricing)
                self.assertIn('name="locale"', pricing)
                for other in PRICING_HEADING.values():
                    if other != PRICING_HEADING[locale]:
                        self.assertNotIn(other, pricing)

    def test_landing_publishing_scope_is_image_posts_and_reels_in_all_locales(self):
        expected = {
            "en": "Image posts and Reels",
            "fr": "Publications image et Reels",
            "ar": "منشورات الصور وريلز",
        }
        for locale, text in expected.items():
            with self.subTest(locale=locale):
                page = self._page("/", locale)
                self.assertIn(text, page)
                self.assertNotIn("Image posts only", page)
                self.assertNotIn("Publications image uniquement", page)
                self.assertNotIn("منشورات الصور فقط", page)

    def test_public_pages_have_no_competitor_mention_in_any_locale(self):
        for path in ("/", "/pricing", "/features/instagram-scheduling", "/features/instagram-automation"):
            for locale in ("en", "fr", "ar"):
                with self.subTest(path=path, locale=locale):
                    page = self._page(path, locale)
                    self.assertNotIn("ManyChat", page)
                    self.assertNotIn("manychat", page)

    def test_feature_pages_are_translated(self):
        expected = {
            "/features/instagram-scheduling": {
                "en": "Plan your Instagram image posts and Reels in one workspace",
                "fr": "Planifiez vos publications image et vos Reels dans un seul espace",
                "ar": "خطط لمنشورات صور إنستغرام وريلز في مساحة عمل واحدة",
            },
            "/features/instagram-automation": {
                "en": "Turn Instagram comments into organized conversations",
                "fr": "Transformez les commentaires Instagram en conversations organisées",
                "ar": "حوّل تعليقات إنستغرام إلى محادثات منظمة",
            },
        }
        for path, headings in expected.items():
            for locale, heading in headings.items():
                with self.subTest(path=path, locale=locale):
                    self.assertIn(heading, self._page(path, locale))

    def test_auth_and_404_pages_are_translated(self):
        self.assertIn("Welcome back", self._page("/login", "en"))
        self.assertIn("Bienvenue", self._page("/login", "fr"))
        self.assertIn("مرحبًا بعودتك", self._page("/login", "ar"))
        self.assertIn("Create your account", self._page("/register", "en"))
        self.assertIn("Créez votre compte", self._page("/register", "fr"))
        self.assertIn("أنشئ حسابك", self._page("/register", "ar"))
        for locale, text in {
            "en": "Page not found",
            "fr": "Page introuvable",
            "ar": "الصفحة غير موجودة",
        }.items():
            with self.subTest(locale=locale):
                _set_locale(self.client, locale)
                response = self.client.get("/no-such-page")
                self.assertEqual(response.status_code, 404)
                self.assertIn(text, response.get_data(as_text=True))

    def test_arabic_pages_use_rtl(self):
        page = self._page("/", "ar")
        self.assertIn('lang="ar" dir="rtl"', page)
        self.assertIn('lang="en" dir="ltr"', self._page("/", "en"))

    def test_public_locale_picker_posts_back_to_the_same_page(self):
        self.client.get("/")
        with self.client.session_transaction() as session:
            csrf = session["_csrf_token"]
        response = self.client.post(
            "/auth/locale",
            data={"locale": "fr", "csrf_token": csrf, "next": "/pricing"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/pricing")
        page = self.client.get("/pricing").get_data(as_text=True)
        self.assertIn(PRICING_HEADING["fr"], page)


class BillingI18nTest(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from auth import configure_auth, install_auth_route_guard, register_auth_routes
        from billing import register_billing
        from database.billing import consume_ai_usage
        from database.models import Base, User

        self._consume_ai_usage = consume_ai_usage
        self.env_patch = patch.dict(os.environ, {
            "BILLING_MODE": "test",
            "LEMON_API_KEY": "test-key",
            "LEMON_STORE_ID": "1",
            "LEMON_WEBHOOK_SECRET": "test-secret",
            "LEMON_PRO_MONTHLY_VARIANT_ID": "10",
            "LEMON_CREDITS_500_VARIANT_ID": "50",
            "LEMON_CREDITS_1200_VARIANT_ID": "51",
            "LEMON_CREDITS_2500_VARIANT_ID": "52",
            "PUBLIC_BASE_URL": "https://postenza.test",
        })
        self.env_patch.start()
        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.tempdir.name) / 'billing-i18n.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        template_dir = ROOT / "dashboard" / "templates"
        static_dir = ROOT / "dashboard" / "static"
        self.app = Flask(__name__, template_folder=str(template_dir), static_folder=str(static_dir))
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="billing-i18n-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_billing(self.app)
        with self.Session.begin() as session:
            self.user = User(
                email="buyer@example.invalid", display_name="Buyer",
                trial_started_at=datetime.now(timezone.utc),
            )
            session.add(self.user)
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.tempdir.cleanup()
        self.env_patch.stop()

    def _login_as(self):
        with self.client.session_transaction() as session:
            session["user_id"] = str(self.user.id)
            session["_csrf_token"] = "csrf-test-token"

    def _billing_page(self, locale):
        self._login_as()
        _set_locale(self.client, locale)
        response = self.client.get("/app/billing")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_billing_heading_plans_and_credit_packs_in_all_locales(self):
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale):
                page = self._billing_page(locale)
                self.assertIn(BILLING_HEADING[locale], page)
                self.assertIn(BUY_500[locale], page)
                self.assertIn('name="locale"', page)
                for other in BILLING_HEADING.values():
                    if other != BILLING_HEADING[locale]:
                        self.assertNotIn(other, page)

    def test_credit_allowance_alerts_are_translated(self):
        warnings = {
            "en": "80% of your monthly AI allowance",
            "fr": "80 % de votre quota mensuel d’IA",
            "ar": "80٪ من حصتك الشهرية",
        }
        exhausted = {
            "en": "both used up",
            "fr": "tous deux épuisés",
            "ar": "نفدت حصتك الشهرية من الذكاء الاصطناعي ورصيدك معًا",
        }
        # Usage is cumulative on the one test user, so consume once per
        # threshold and then check every locale against the same state.
        with self.Session.begin() as session:
            for _ in range(240):
                self._consume_ai_usage(session, self.user.id)
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale, state="warning"):
                self.assertIn(warnings[locale], self._billing_page(locale))
        with self.Session.begin() as session:
            for _ in range(60):
                self._consume_ai_usage(session, self.user.id)
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale, state="exhausted"):
                self.assertIn(exhausted[locale], self._billing_page(locale))


class DashboardI18nTest(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from auth import configure_auth, install_auth_route_guard, register_auth_routes
        from database.models import Base, InstagramAccount, InstagramCredential, User
        from tenant_dashboard import register_tenant_dashboard

        self.tempdir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.tempdir.name) / 'tenant-i18n.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        template_dir = ROOT / "dashboard" / "templates"
        static_dir = ROOT / "dashboard" / "static"
        self.app = Flask(__name__, template_folder=str(template_dir), static_folder=str(static_dir))
        self.app.config["TESTING"] = True
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="tenant-i18n-test-secret",
            legacy_instagram_user_id="90000000000000001",
            production=False,
        )
        self.app.add_url_rule("/dashboard", "dashboard", lambda: "legacy dashboard")
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        register_tenant_dashboard(self.app)
        now = datetime.now(timezone.utc)
        with self.Session.begin() as session:
            self.owner = User(email="owner@example.invalid", display_name="Owner")
            session.add(self.owner)
            session.flush()
            account = InstagramAccount(
                user_id=self.owner.id,
                instagram_user_id="10000000000000001",
                username="alpha_first",
                connection_status="connected",
                created_at=now - timedelta(minutes=2),
            )
            session.add(account)
            session.flush()
            session.add(InstagramCredential(
                instagram_account_id=account.id,
                token_ciphertext="ciphertext-secret",
                encryption_key_id="KEY_2026_01",
                scopes=["instagram_business_basic"],
                token_status="valid",
            ))
        self.client = self.app.test_client()

    def tearDown(self):
        factory = self.app.extensions["phase3a_auth"].session_factory
        if factory is not None:
            factory.kw["bind"].dispose()
        self.engine.dispose()
        self.tempdir.cleanup()

    def _login_as(self):
        with self.client.session_transaction() as session:
            session["user_id"] = str(self.owner.id)
            session["_csrf_token"] = "csrf-test-token"

    def _page(self, path, locale):
        self._login_as()
        _set_locale(self.client, locale)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_dashboard_overview_headings_in_all_locales(self):
        for locale in ("en", "fr", "ar"):
            with self.subTest(locale=locale):
                page = self._page("/app", locale)
                self.assertIn(OVERVIEW_HEADING[locale], page)
                self.assertIn('name="locale"', page)
                for other in OVERVIEW_HEADING.values():
                    if other != OVERVIEW_HEADING[locale]:
                        self.assertNotIn(f">{other}<", page)

    def test_content_composer_reel_strings_in_all_locales(self):
        expected = {
            "en": ("Upload a video", "Image and Reel publishing", "Cover image (optional)"),
            "fr": ("Importer une vidéo", "Publication d’images et de Reels", "Image de couverture (facultative)"),
            "ar": ("تحميل فيديو", "نشر الصور وريلز", "صورة الغلاف (اختيارية)"),
        }
        for locale, texts in expected.items():
            with self.subTest(locale=locale):
                page = self._page("/app/content", locale)
                for text in texts:
                    self.assertIn(text, page)
                self.assertNotIn("Upload a video", page) if locale != "en" else None

    def test_dashboard_sidebar_nav_in_all_locales(self):
        expected = {
            "en": ("Content", "Calendar", "Engagement", "Automation", "Settings", "Billing"),
            "fr": ("Contenu", "Calendrier", "Interactions", "Automatisation", "Paramètres", "Facturation"),
            "ar": ("المحتوى", "التقويم", "التفاعل", "الأتمتة", "الإعدادات", "الفوترة"),
        }
        for locale, labels in expected.items():
            with self.subTest(locale=locale):
                page = self._page("/app", locale)
                for label in labels:
                    self.assertIn(f">{label}</a>", page)


class TranslationCompletenessTest(unittest.TestCase):
    def test_every_template_key_resolves_in_all_locales(self):
        from dashboard.app_translations import APP_TRANSLATIONS

        pattern = re.compile(r"""\bt\(\s*['"](.+?)['"]""", re.S)
        templates = ROOT / "dashboard" / "templates"
        for path in sorted(templates.glob("*.html")):
            if path.name == "dashboard.html":
                continue  # retired legacy template, no longer rendered
            for match in pattern.finditer(path.read_text()):
                key = match.group(1)
                if "href=" in key:
                    continue  # regex artifact of the register legal-note key
                for locale in ("en", "fr", "ar"):
                    with self.subTest(template=path.name, locale=locale, key=key):
                        # The key must exist in that locale's table: translate()
                        # falls back to the raw English key when it is missing,
                        # which would leak English into the French/Arabic page.
                        self.assertTrue(
                            key in APP_TRANSLATIONS[locale]
                            or key in LANDING_TRANSLATIONS[locale]
                            or key in TRANSLATIONS[locale],
                            f"{key!r} has no {locale} translation",
                        )

    def test_translation_tables_share_the_same_keys(self):
        self.assertEqual(set(TRANSLATIONS["en"]), set(TRANSLATIONS["fr"]))
        self.assertEqual(set(TRANSLATIONS["en"]), set(TRANSLATIONS["ar"]))
        self.assertEqual(set(LANDING_TRANSLATIONS["en"]), set(LANDING_TRANSLATIONS["fr"]))
        self.assertEqual(set(LANDING_TRANSLATIONS["en"]), set(LANDING_TRANSLATIONS["ar"]))

    def test_reel_and_credit_keys_are_translated(self):
        for key in (
            "Upload a video",
            "Cover image (optional)",
            "Instagram could not publish this Reel. Check the video link and Instagram before creating a new post.",
            "Buy {count} credits",
            "You have used 80% of your monthly AI allowance. Further AI uses will draw on your credits once the allowance runs out.",
            "Your monthly AI allowance and your credits are both used up. Buy credits below or upgrade your plan to continue using AI.",
            "Credit purchase is not available yet — the one-time variants still have to be set up by hand in Lemon Squeezy.",
        ):
            for locale in ("fr", "ar"):
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(translate(locale, key), key)
                    self.assertNotEqual(translate(locale, key), translate("en", key))


if __name__ == "__main__":
    unittest.main()
