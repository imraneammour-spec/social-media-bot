"""Focused tests for the shared Postenza auth/onboarding shell polish.

Covers the shared shell, the auth locale selector, registration legal
context, honest support-assisted recovery, and unchanged core auth
behavior (rotation, CSRF, validation).
"""

import re
import tempfile
import unittest
from pathlib import Path

from flask import Flask
from jinja2 import Environment, nodes
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import generate_password_hash

from auth import (
    SUPPORT_EMAIL,
    configure_auth,
    install_auth_route_guard,
    register_auth_routes,
)
from dashboard.app_translations import APP_TRANSLATIONS, _MESSAGES
from dashboard.i18n import LOCALE_META, TRANSLATIONS
from database.models import Base, InstagramAccount, User


ROOT = Path(__file__).resolve().parents[1]
LEGAL_ACKNOWLEDGEMENT = (
    "By creating an account, you agree to the "
    '<a href="{terms_url}">Terms</a> and acknowledge the '
    '<a href="{privacy_url}">Privacy Policy</a>.'
)


class AuthShellPolishTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_url = f"sqlite:///{Path(self.temp_dir.name) / 'auth_shell.db'}"
        self.engine = create_engine(self.database_url)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.app = Flask(
            __name__,
            template_folder=str(ROOT / "dashboard" / "templates"),
        )
        configure_auth(
            self.app,
            database_url=self.database_url,
            secret_key="auth-shell-test-secret",
            legacy_instagram_user_id="legacy-account-id",
            production=False,
        )
        register_auth_routes(self.app)
        install_auth_route_guard(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        auth_factory = self.app.extensions["phase3a_auth"].session_factory
        if auth_factory is not None:
            auth_factory.kw["bind"].dispose()
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_user(self, email, password="correct horse battery staple"):
        with self.Session.begin() as session:
            user = User(
                email=email,
                display_name=email.split("@", 1)[0],
                password_hash=generate_password_hash(password),
            )
            session.add(user)
            session.flush()
            return user.id

    def _session_csrf(self):
        with self.client.session_transaction() as browser_session:
            token = browser_session.get("_csrf_token")
            if not token:
                token = "auth-shell-csrf-token-with-32-bytes-ok"
                browser_session["_csrf_token"] = token
            return token

    def _page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def _login_session(self, email):
        with self.Session.begin() as session:
            user = session.query(User).filter_by(email=email).one()
            user_id = str(user.id)
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = user_id
    # 1-3. Shared shell ---------------------------------------------------

    def test_login_and_register_render_the_shared_postenza_auth_shell(self):
        for path in ("/login", "/register"):
            page = self._page(path)
            self.assertIn("Postenza", page)
            self.assertIn("postenza-logo-light.png", page)
            self.assertIn('alt="Postenza"', page)
            self.assertIn('href="/static/auth.css"', page)
            self.assertIn("auth-card", page)
            self.assertIn('class="skip-link" href="#main-content"', page)
            self.assertIn('action="/auth/locale"', page)
            self.assertIn('href="/terms"', page)
            self.assertIn('href="/privacy"', page)
            self.assertNotIn("Sociora", page)

    def test_onboarding_renders_the_shared_postenza_auth_shell(self):
        self._create_user("shell-onboarding@example.test")
        self._login_session("shell-onboarding@example.test")

        page = self._page("/onboarding")

        self.assertIn("Postenza", page)
        self.assertIn("postenza-logo-light.png", page)
        self.assertIn('href="/static/auth.css"', page)
        self.assertIn("auth-card", page)
        self.assertIn('class="skip-link" href="#main-content"', page)
        self.assertIn('action="/auth/locale"', page)
        self.assertIn("No Instagram account connected", page)
        self.assertIn('method="post" action="/connect/instagram"', page)
        self.assertIn('method="post" action="/logout"', page)
        self.assertIn('href="/terms"', page)
        self.assertIn('href="/privacy"', page)

    # 4. Language selector on all three pages -----------------------------

    def test_language_selector_appears_on_all_three_pages(self):
        self._create_user("selector@example.test")
        for path in ("/login", "/register"):
            page = self._page(path)
            self.assertIn('action="/auth/locale"', page, path)
            self.assertIn('name="csrf_token"', page, path)
            self.assertIn('name="next"', page, path)
            for code in LOCALE_META:
                self.assertIn('<option value="' + code + '"', page, path)

        self._login_session("selector@example.test")
        page = self._page("/onboarding")
        self.assertIn('action="/auth/locale"', page, "/onboarding")
        self.assertIn('name="csrf_token"', page, "/onboarding")
        self.assertIn('name="next"', page, "/onboarding")
        for code in LOCALE_META:
            self.assertIn('<option value="' + code + '"', page, "/onboarding")

    # 5-7. Locale change behavior -----------------------------------------

    def test_locale_change_returns_to_the_current_page(self):
        csrf = self._session_csrf()
        self.client.get("/register")

        response = self.client.post(
            "/auth/locale",
            data={"csrf_token": csrf, "locale": "fr", "next": "/register"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/register")
        page = self.client.get("/register").get_data(as_text=True)
        self.assertIn('<html lang="fr" dir="ltr">', page)
        self.assertIn("Créez votre compte", page)

    def test_arbitrary_and_external_locale_return_destinations_are_rejected(self):
        csrf = self._session_csrf()
        self.client.get("/login")
        hostile_destinations = (
            "https://evil.example/steal",
            "//evil.example/steal",
            "/\\evil.example",
            "http://localhost/steal",
            "\\/login",
            "relative/path",
            "javascript:alert(1)",
            "",
        )

        for destination in hostile_destinations:
            response = self.client.post(
                "/auth/locale",
                data={"csrf_token": csrf, "locale": "en", "next": destination},
            )
            self.assertEqual(response.status_code, 303, destination)
            self.assertEqual(response.headers["Location"], "/login", destination)

    def test_locale_endpoint_requires_csrf(self):
        self.client.get("/login")

        missing = self.client.post("/auth/locale", data={"locale": "fr", "next": "/login"})
        self.assertEqual(missing.status_code, 400)
        wrong = self.client.post(
            "/auth/locale",
            data={"csrf_token": "not-the-token", "locale": "fr", "next": "/login"},
        )
        self.assertEqual(wrong.status_code, 400)
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('<html lang="en" dir="ltr">', page)

    def test_locale_endpoint_rejects_unsupported_locales(self):
        csrf = self._session_csrf()
        self.client.get("/login")

        response = self.client.post(
            "/auth/locale",
            data={"csrf_token": csrf, "locale": "de", "next": "/login"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/login")
        page = self.client.get("/login").get_data(as_text=True)
        self.assertIn('<html lang="en" dir="ltr">', page)
    # 8. RTL --------------------------------------------------------------

    def test_arabic_locale_renders_right_to_left(self):
        with self.client.session_transaction() as browser_session:
            browser_session["postenza_locale"] = "ar"

        for path in ("/login", "/register"):
            page = self._page(path)
            self.assertIn('<html lang="ar" dir="rtl">', page)

    # 9-10. Registration legal context ------------------------------------

    def test_registration_offers_working_terms_and_privacy_links(self):
        page = self._page("/register")

        self.assertIn('href="/terms"', page)
        self.assertIn('href="/privacy"', page)
        self.assertIn("By creating an account", page)
        # Normal browser flow: no popups or forced new tabs.
        self.assertNotIn('target="_blank"', page)
        self.assertNotIn("window.open", page)

    def test_registration_legal_acknowledgement_has_translation_parity(self):
        keys = {row[0] for row in _MESSAGES}
        self.assertIn(LEGAL_ACKNOWLEDGEMENT, keys)
        for locale in ("en", "fr", "ar"):
            translation = APP_TRANSLATIONS[locale][LEGAL_ACKNOWLEDGEMENT]
            self.assertTrue(translation.strip(), locale)
            self.assertIn("terms_url}", translation)
            self.assertIn("privacy_url}", translation)
        template_keys = set()
        for name in ("auth_shell.html", "login.html", "register.html", "onboarding.html"):
            parsed = Environment().parse(
                (ROOT / "dashboard" / "templates" / name).read_text(encoding="utf-8")
            )
            for call in parsed.find_all(nodes.Call):
                if (
                    isinstance(call.node, nodes.Name)
                    and call.node.name == "t"
                    and call.args
                    and isinstance(call.args[0], nodes.Const)
                ):
                    template_keys.add(call.args[0].value)
        for locale in ("en", "fr", "ar"):
            missing = template_keys - APP_TRANSLATIONS[locale].keys() - TRANSLATIONS[locale].keys()
            self.assertFalse(missing, (locale, sorted(missing)))
    # 11-12. Honest recovery ----------------------------------------------

    def test_login_does_not_offer_a_fake_password_reset(self):
        page = self._page("/login")
        lowered = page.lower()

        self.assertNotIn("forgot", lowered)
        self.assertNotIn("reset", lowered)
        self.assertNotIn("/auth/reset", page)
        self.assertNotIn("reset_password", page)
        # Only the login form and the locale selector may POST from here.
        actions = set(re.findall(r'action="([^"]+)"', page))
        self.assertEqual(actions, {"/login", "/auth/locale"})

    def test_support_recovery_references_the_existing_support_contact(self):
        privacy_text = (ROOT / "privacy.html").read_text(encoding="utf-8")
        legal_emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", privacy_text))

        self.assertIn(SUPPORT_EMAIL, legal_emails)
        page = self._page("/login")
        self.assertIn(SUPPORT_EMAIL, page)
        self.assertIn("Contact support", page)

    # 13. Core authentication behavior remains unchanged -------------------

    def test_login_still_authenticates_rotates_session_and_keeps_locale(self):
        user_id = self._create_user("rotation@example.test")
        pre_login_csrf = "rotation-csrf-token-with-32-bytes-ok"
        with self.client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = pre_login_csrf
            browser_session["postenza_locale"] = "fr"

        response = self.client.post(
            "/login",
            data={
                "csrf_token": pre_login_csrf,
                "email": "rotation@example.test",
                "password": "correct horse battery staple",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/onboarding"))
        with self.client.session_transaction() as browser_session:
            self.assertEqual(str(user_id), browser_session["user_id"])
            self.assertNotEqual(pre_login_csrf, browser_session["_csrf_token"])
            self.assertEqual("fr", browser_session["postenza_locale"])
            rotated_csrf = browser_session["_csrf_token"]
        wrong_password = self.client.post(
            "/login",
            data={
                "csrf_token": rotated_csrf,
                "email": "rotation@example.test",
                "password": "wrong password entirely",
            },
        )
        # The pre-login locale (fr) must survive session rotation.
        self.assertIn("Adresse e-mail ou mot de passe incorrect.", wrong_password.get_data(as_text=True))

    def test_onboarding_locale_change_returns_to_onboarding(self):
        self._create_user("onboard-locale@example.test")
        self._login_session("onboard-locale@example.test")
        self.client.get("/onboarding")
        csrf = self._session_csrf()

        response = self.client.post(
            "/auth/locale",
            data={"csrf_token": csrf, "locale": "fr", "next": "/onboarding"},
        )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/onboarding")
        page = self.client.get("/onboarding").get_data(as_text=True)
        self.assertIn('<html lang="fr" dir="ltr">', page)


if __name__ == "__main__":
    unittest.main()