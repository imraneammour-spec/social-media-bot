"""Regenerate Postenza v2 previews (landing EN/FR/AR, pricing, dashboard
overview) from the local app, without GEMINI_API_KEY and without deploying.

Usage: .venv-test/bin/python scripts/generate_previews.py
Writes into ~/workspace/postenza-deploy/previews/.

Every preview is SELF-CONTAINED: all <link rel="stylesheet"> tags are
replaced by an inline <style> with the file contents, and every
/static/brand image (logo, icon, favicon) is replaced by a base64 data URI
of a downscaled copy (logos max 320px wide, icons max 96px). That is what
makes a preview open correctly on a phone from a standalone .html file,
where /static/... URLs resolve to nothing. The live app itself is NOT
changed: it keeps serving /static/... normally.
"""
import base64
import importlib.util
import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "dashboard" / "static"
OUT = Path.home() / "workspace/postenza-deploy/previews"
OUT.mkdir(parents=True, exist_ok=True)

_CSS_CACHE = {}
_IMAGE_CACHE = {}


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.name = kwargs.get("name", "")

    def start(self):
        return None

    def is_alive(self):
        return False


def _inline_css(html):
    """Replace <link rel="stylesheet" href="/static/X.css"> with <style>."""

    def repl(match):
        rel = match.group(1)
        if rel not in _CSS_CACHE:
            _CSS_CACHE[rel] = (STATIC_DIR / rel).read_text(encoding="utf-8")
        css = _CSS_CACHE[rel]
        assert "</style" not in css.lower(), f"unsafe CSS in {rel}"
        return f"<style>\n/* inlined {rel} for standalone preview */\n{css}\n</style>"

    return re.sub(
        r'<link rel="stylesheet" href="/static/([^"]+\.css)">', repl, html
    )


def _data_uri_image(rel_path):
    """Base64 data URI of a downscaled brand image (logos <=320px, icons <=96px)."""
    if rel_path not in _IMAGE_CACHE:
        from PIL import Image

        path = STATIC_DIR / rel_path
        image = Image.open(path)
        max_width = 96 if "icon" in Path(rel_path).name else 320
        if image.width > max_width:
            image.thumbnail((max_width, 10000))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        _IMAGE_CACHE[rel_path] = (
            "data:image/png;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
    return _IMAGE_CACHE[rel_path]


def _inline_images(html):
    """Replace /static/brand images with base64 data URIs.

    The favicon <link rel="icon"> tags are collapsed to a single data-URI
    favicon (the 32px one, 2.7KB); <img> logos/icons get a downscaled copy.
    """

    def icon_repl(match):
        if icon_repl.used:
            return ""
        icon_repl.used = True
        return (
            '<link rel="icon" href="'
            + _data_uri_image("brand/postenza-favicon-32.png")
            + '">'
        )

    icon_repl.used = False
    html = re.sub(
        r'<link rel="icon"[^>]*href="/static/[^"]*"[^>]*>', icon_repl, html
    )

    def img_repl(match):
        prefix, rel, suffix = match.groups()
        return f'{prefix}src="{_data_uri_image(rel)}"{suffix}'

    html = re.sub(
        r'(<img[^>]*?)src="/static/(brand/[^"]+\.png)"([^>]*>)',
        img_repl,
        html,
    )
    # og:image is only metadata (browsers do not fetch it on plain view),
    # but it still points at /static/... — inline it the same way.
    html = re.sub(
        r'<meta property="og:image" content="[^"]*/static/(brand/[^"]+\.png)">',
        lambda m: '<meta property="og:image" content="'
        + _data_uri_image(m.group(1))
        + '">',
        html,
    )
    return html


def make_self_contained(html):
    """Inline all CSS and brand images so the file renders standalone."""
    html = _inline_css(html)
    html = _inline_images(html)
    return html


def load_webhook():
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    tmp = tempfile.mkdtemp()
    env = {
        "DATABASE_URL": f"sqlite:///{Path(tmp) / 'preview.db'}",
        "FLASK_SECRET_KEY": "preview-secret",
        "VERIFY_TOKEN": "preview-verify-token",
        "META_APP_SECRET": "",
        "APP_ENV": "test",
        "ALLOW_UNSIGNED_WEBHOOKS": "1",
        "INSTAGRAM_ACCESS_TOKEN": "",
        "LEGACY_INSTAGRAM_USER_ID": "17841438617458289",
        "GEMINI_API_KEY": "",
    }
    name = "postenza_preview_app"
    spec = importlib.util.spec_from_file_location(name, ROOT / "webhook.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    with (
        patch.dict(os.environ, env, clear=False),
        patch("threading.Thread", _NoopThread),
        redirect_stdout(io.StringIO()),
    ):
        sys.modules.pop("legacy_bot", None)
        spec.loader.exec_module(module)
    return module


def main():
    module = load_webhook()
    client = module.app.test_client()

    def page_for_locale(path, locale):
        client.get("/")
        with client.session_transaction() as session:
            csrf = session["_csrf_token"]
        resp = client.post("/locale", data={"locale": locale, "csrf_token": csrf}, follow_redirects=False)
        assert resp.status_code == 303, (path, locale, resp.status_code)
        resp = client.get(path)
        assert resp.status_code == 200, (path, locale, resp.status_code)
        return resp.get_data(as_text=True)

    def write_preview(filename, html):
        html = make_self_contained(html)
        assert "<style>" in html, filename
        assert 'rel="stylesheet"' not in html, filename
        assert '"/static/' not in html, filename
        assert "data:image/png;base64," in html, filename
        (OUT / filename).write_text(html)

    # Landing EN/FR/AR (same filenames as the previous v2 previews).
    write_preview("landing-v2-preview.html", page_for_locale("/", "en"))
    write_preview("landing-v2-preview-fr.html", page_for_locale("/", "fr"))
    write_preview("landing-v2-preview-ar.html", page_for_locale("/", "ar"))
    print("landing previews written")
    # Pricing page: set the session locale directly (its picker posts to
    # /auth/locale, which is also exercised by the test suite).
    for locale, filename in (("en", "pricing-v2-preview.html"),
                             ("fr", "pricing-v2-preview-fr.html"),
                             ("ar", "pricing-v2-preview-ar.html")):
        with client.session_transaction() as session:
            session["postenza_locale"] = locale
        resp = client.get("/pricing")
        assert resp.status_code == 200, (locale, resp.status_code)
        write_preview(filename, resp.get_data(as_text=True))
    print("pricing previews written")
    # Dashboard overview: needs a logged-in tenant user; reuse the tenant
    # test harness pieces (sqlite + auth + tenant dashboard).
    from datetime import datetime, timedelta, timezone

    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from auth import configure_auth, install_auth_route_guard, register_auth_routes
    from database.models import Base, InstagramAccount, InstagramCredential, User
    from tenant_dashboard import register_tenant_dashboard

    tmp = tempfile.mkdtemp()
    database_url = f"sqlite:///{Path(tmp) / 'preview-tenant.db'}"
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    app = Flask(__name__, template_folder=str(ROOT / "dashboard" / "templates"),
                static_folder=str(ROOT / "dashboard" / "static"))
    app.config["TESTING"] = True
    configure_auth(app, database_url=database_url, secret_key="preview-tenant-secret",
                   legacy_instagram_user_id="90000000000000001", production=False)
    app.add_url_rule("/dashboard", "dashboard", lambda: "legacy dashboard")
    register_auth_routes(app)
    install_auth_route_guard(app)
    register_tenant_dashboard(app)
    now = datetime.now(timezone.utc)
    with Session.begin() as session:
        owner = User(email="owner@example.invalid", display_name="Owner")
        session.add(owner)
        session.flush()
        account = InstagramAccount(user_id=owner.id, instagram_user_id="10000000000000001",
                                   username="alpha_first", connection_status="connected",
                                   created_at=now - timedelta(minutes=2))
        session.add(account)
        session.flush()
        session.add(InstagramCredential(instagram_account_id=account.id,
                                        token_ciphertext="ciphertext-secret",
                                        encryption_key_id="KEY_2026_01",
                                        scopes=["instagram_business_basic"],
                                        token_status="valid"))
        owner_id = str(owner.id)
    tclient = app.test_client()
    for locale, filename in (("en", "dashboard-overview-preview.html"),
                             ("fr", "dashboard-overview-preview-fr.html"),
                             ("ar", "dashboard-overview-preview-ar.html")):
        with tclient.session_transaction() as session:
            session["user_id"] = owner_id
            session["_csrf_token"] = "csrf-test-token"
            session["postenza_locale"] = locale
        resp = tclient.get("/app")
        assert resp.status_code == 200, (locale, resp.status_code)
        write_preview(filename, resp.get_data(as_text=True))
    print("dashboard overview previews written")
    engine.dispose()


if __name__ == "__main__":
    main()
