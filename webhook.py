import os
import re
import json
import html
import hashlib
import hmac
import threading
import uuid
import time
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from io import BytesIO
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from dotenv import load_dotenv
from flask import Flask, request, send_file, Response, url_for, render_template, redirect, session
from werkzeug.utils import secure_filename

from database.cli import register_database_commands
from database.config import database_url_from_environment
from database.account_runtime import (
    InstagramAccountRoute,
    RuntimeAccountRouteUnavailableError,
    resolve_instagram_account_route,
)
from database.oauth_engagement import (
    claim_oauth_event,
    complete_oauth_event,
    consume_oauth_direct_message_interaction,
    get_oauth_pending_access,
    get_oauth_prompt_post,
    get_oauth_prompt_post_by_public_id,
    oauth_prompt_post,
    oauth_request_on_cooldown,
    oauth_smart_reply_allowed,
    pop_oauth_pending_access,
    record_oauth_request,
    record_oauth_smart_reply,
    record_oauth_direct_message_interaction,
    save_oauth_pending_access,
)
from database.oauth_graph import (
    lookup_oauth_media,
    oauth_engagement_capability_state,
    send_oauth_delayed_followup,
    send_oauth_private_comment_reply,
    send_oauth_private_access_reply,
    send_oauth_prompt_access_button,
    send_oauth_public_comment_reply,
)
from database.oauth_followups import (
    DIRECT_MESSAGE_RESPONSE_WINDOW,
    cancel_oauth_followups,
    claim_due_oauth_followups,
    finalize_oauth_followup_failure,
    finalize_oauth_followup_success,
    oauth_followup_account_external_id,
    reap_expired_oauth_followup_leases,
    schedule_oauth_followup,
)
from database.oauth_webhook_ingress import (
    claim_due_oauth_webhook_work,
    complete_oauth_webhook_work,
    enqueue_oauth_webhook_work,
    fail_oauth_webhook_work,
    oauth_webhook_work_account_matches,
)
from comment_pipeline import generic_ready_reply
from database.tenant_automation import (
    DEFAULT_CUSTOM_MESSAGE,
    oauth_automation_runtime_config,
)
from auth import (
    configure_auth,
    csrf_token,
    validate_csrf_token,
    get_current_user,
    install_auth_route_guard,
    register_auth_routes,
    send_trial_reminder_email,
    verify_meta_signature,
    webhook_signature_verification_active,
)
from dashboard.i18n import LOCALE_META, LOCALE_SESSION_KEY, locale_context, normalize_locale, translate, session_locale
from tenant_dashboard import register_tenant_dashboard
from instagram_oauth import validated_public_origin

from google.genai import types

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    PIL_AVAILABLE = True
except Exception:
    Image = ImageDraw = ImageFont = ImageFilter = None
    PIL_AVAILABLE = False


# ==================================================
# CONFIG
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Persistent data:
# - On Railway, the mounted volume is /app/data.
# - Locally, keep using the project's data folder.


# Story and follow-up scheduler configuration. Posts remain manual-only.

load_dotenv(ENV_FILE)

# The legacy bot lives in legacy_bot.py; re-exported here so existing
# callers and tests keep working against the webhook module.
from legacy_bot import (
    API_VERSION,
    AUTO_STORY_SETTINGS_FILE,
    DATA_DIR,
    DRAFTS_FILE,
    DRAFT_IMAGES_DIR,
    FOLLOWUPS_FILE,
    FOLLOWUP_COOLDOWN_DAYS,
    FOLLOWUP_DELAY_HOURS,
    FOLLOWUP_MAX_AGE_HOURS,
    INSTAGRAM_ACCESS_TOKEN,
    INSTAGRAM_USER_ID,
    LEGACY_INSTAGRAM_USER_ID,
    PENDING_ACCESS_FILE,
    POSTS_FILE,
    PROCESSED_FILE,
    PUBLIC_REPLY_MESSAGES,
    RAILWAY_DATA_DIR,
    REQUESTS_FILE,
    REQUEST_COOLDOWN_MINUTES,
    SMART_REPLY_ACTIVITY_FILE,
    SMART_REPLY_MAX_LENGTH,
    SMART_REPLY_MAX_PER_HOUR,
    SMART_REPLY_MIN_INTERVAL_SECONDS,
    STATE_LOCK,
    STORIES_FILE,
    TRIGGER_WORDS,
    _parse_iso_datetime,
    aggregate_lead_profiles,
    cancel_pending_followups,
    claim_comment,
    clear_pending_access,
    configure_legacy_state_session_factory,
    followup_due,
    generate_smart_reply,
    get_or_create_post,
    get_pending_access,
    get_saved_post,
    is_request_on_cooldown,
    load_followups,
    load_json_file,
    load_posts,
    load_processed_comments,
    load_requests,
    load_smart_reply_activity,
    log_prompt_request,
    mark_comment_done,
    process_comment,
    process_due_followups,
    process_message_event,
    process_smart_comment,
    record_smart_reply,
    release_comment,
    request_identity,
    save_json_file,
    save_pending_access,
    save_post,
    schedule_prompt_followup,
    send_access_request,
    send_comment_reply_text,
    send_followup_message,
    send_public_comment_reply,
    send_view_prompts_button,
    should_smart_reply,
    smart_reply_rate_allowed,
)

# Dashboard scheduler configuration. These stay top-level assignments in
# webhook.py (not in legacy_bot.py): the scheduler loop lives here, and
# tests/test_scheduler_configuration.py extracts them from this file by AST.
SCHEDULER_TIMEZONE = ZoneInfo("Africa/Casablanca")
SCHEDULER_POLL_SECONDS = 15
SCHEDULER_LOCKS_DIR = DATA_DIR / "scheduler_locks"
SCHEDULER_LOCKS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

# Optional Phase 2 configuration only.  No engine, session, table creation, or
# JSON-to-SQL migration occurs during application startup.
DATABASE_URL = database_url_from_environment()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

CLOUDINARY_CLOUD_NAME = os.getenv(
    "CLOUDINARY_CLOUD_NAME",
    ""
).strip()

CLOUDINARY_API_KEY = os.getenv(
    "CLOUDINARY_API_KEY",
    ""
).strip()

CLOUDINARY_API_SECRET = os.getenv(
    "CLOUDINARY_API_SECRET",
    ""
).strip()


# Phase 3A auth is additive.  If this deployment has not yet configured both
# DATABASE_URL and FLASK_SECRET_KEY, public compatibility routes keep working
# while dashboard/auth access safely remains unavailable.
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
APP_ENVIRONMENT = os.getenv("APP_ENV", os.getenv("FLASK_ENV", "")).strip().lower()
IS_PRODUCTION = APP_ENVIRONMENT in {"production", "prod"} or bool(
    os.getenv("RAILWAY_ENVIRONMENT_NAME", "").strip()
)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
ALLOW_UNSIGNED_WEBHOOKS = os.getenv("ALLOW_UNSIGNED_WEBHOOKS", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
WEBHOOK_UNSIGNED_COMPATIBILITY = bool(
    not IS_PRODUCTION
    and APP_ENVIRONMENT in {"development", "dev", "test", "testing"}
    and ALLOW_UNSIGNED_WEBHOOKS
)
configure_auth(
    app,
    database_url=DATABASE_URL,
    secret_key=FLASK_SECRET_KEY,
    legacy_instagram_user_id=INSTAGRAM_USER_ID,
    production=IS_PRODUCTION,
)
app.config["META_APP_SECRET"] = META_APP_SECRET
app.config["WEBHOOK_UNSIGNED_COMPATIBILITY"] = WEBHOOK_UNSIGNED_COMPATIBILITY
register_auth_routes(app)
install_auth_route_guard(app)
register_tenant_dashboard(app)
from billing import register_billing
register_billing(app)
from operations import register_operations
register_operations(app)
from seo import register_seo
register_seo(app)

# The command is explicit (``flask --app webhook bootstrap-owner``) and lazy:
# registering it neither connects to PostgreSQL nor changes Phase 0 JSON state.
register_database_commands(app, lambda: INSTAGRAM_USER_ID)


# Provider CDN cover URLs are presentation metadata rather than durable media.
# Refresh them at most once every six hours instead of on every public request.
OAUTH_PROMPT_COVER_METADATA_TTL = timedelta(hours=6)

AUTO_STORY_DAILY_ENABLED = True
AUTO_STORY_DAILY_TIME = "20:00"
AUTO_STORY_REPEAT_AFTER_DAYS = 7
AUTO_STORY_MIN_GAP_HOURS = 6


# Gemini lives in gemini_client.py; re-exported here so existing callers
# and tests keep working against the webhook module.
from gemini_client import (
    GEMINI_CLIENT,
    GEMINI_FALLBACK_MODEL,
    GEMINI_MODEL,
    _gemini_generate_content,
    _gemini_generate_with_retry,
    _sanitized_gemini_error,
    build_gemini_client,
)



# ==================================================
# STARTUP CHECKS
# ==================================================

print("========================================")
print("Postenza")
print("========================================")

if INSTAGRAM_ACCESS_TOKEN:
    print("Instagram access token: OK")
else:
    print("INFO: Optional legacy Instagram integration: not configured")

if GEMINI_API_KEY:
    print("Gemini API key: OK")
else:
    print("ERROR: GEMINI_API_KEY missing")

if (
    CLOUDINARY_CLOUD_NAME
    and CLOUDINARY_API_KEY
    and CLOUDINARY_API_SECRET
):
    print("Cloudinary media hosting: OK")
else:
    print("WARNING: Cloudinary media hosting is not configured")


# ==================================================
# BASIC ROUTES
# ==================================================

@app.route("/", methods=["GET"])
def home():
    locale = locale_context(session_locale(session))
    public_origin = validated_public_origin(PUBLIC_BASE_URL)
    if public_origin is None and IS_PRODUCTION:
        public_origin = "https://postenza.app"
    canonical_url = (
        f"{public_origin}/"
        if public_origin is not None
        else url_for("home", _external=True)
    )
    og_image_url = (
        f"{public_origin}/static/brand/postenza-logo-light.png"
        if public_origin is not None
        else url_for(
            "static",
            filename="brand/postenza-logo-light.png",
            _external=True,
        )
    )
    return render_template(
        "landing.html",
        canonical_url=canonical_url,
        csrf_token=csrf_token() if app.secret_key else "",
        locale=locale,
        locale_options=LOCALE_META,
        og_image_url=og_image_url,
        t=lambda key: translate(locale["code"], key),
    )


@app.errorhandler(404)
def page_not_found(error):
    """Return the branded Postenza 404 page instead of the bare Flask one."""

    locale = locale_context(session_locale(session))
    return render_template(
        "not_found.html",
        csrf_token=csrf_token() if app.secret_key else "",
        locale=locale,
        locale_options=LOCALE_META,
        t=lambda key: translate(locale["code"], key),
    ), 404


@app.route("/sitemap.xml", methods=["GET"])
def sitemap():
    from seo import PUBLIC_PAGES, origin
    from xml.sax.saxutils import escape
    urls = "".join(f"<url><loc>{escape(origin() + path)}</loc></url>" for path in PUBLIC_PAGES)
    return Response('<?xml version="1.0" encoding="UTF-8"?>' +
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>',
        mimetype="application/xml")


@app.route("/robots.txt", methods=["GET"])
def robots():
    from seo import origin
    if os.getenv("SEARCH_INDEXING_ENABLED", "true").lower() != "true":
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")
    private = ["/app", "/dashboard", "/login", "/register", "/logout", "/onboarding", "/auth/", "/connect/", "/disconnect/", "/oauth/", "/webhook", "/billing/", "/api/", "/health", "/ready", "/p/"]
    content = "User-agent: *\nAllow: /\n" + "".join("Disallow: " + path + "\n" for path in private)
    return Response(content + "\nSitemap: " + origin() + "/sitemap.xml\n", mimetype="text/plain")


@app.route("/health", methods=["GET"])
def health():
    """Return a constant liveness response without touching application state."""

    return {"status": "ok"}, 200


@app.route("/locale", methods=["POST"])
def landing_locale():
    """Store only an allowlisted public UI locale and return to the landing page."""

    if not validate_csrf_token(request.form.get("csrf_token")):
        return "Invalid form token.", 400
    session[LOCALE_SESSION_KEY] = normalize_locale(request.form.get("locale"))
    return redirect(url_for("home"), code=303)


@app.route("/privacy", methods=["GET"])
def privacy():

    return send_file(
        BASE_DIR / "privacy.html"
    )


@app.route("/terms", methods=["GET"])
def terms():

    return send_file(
        BASE_DIR / "terms.html"
    )


@app.route("/deletion", methods=["GET"])
def deletion():

    return send_file(
        BASE_DIR / "deletion.html"
    )


# ==================================================
# WEBHOOK VERIFICATION
# ==================================================

@app.route("/webhook", methods=["GET"])
def verify_webhook():

    if not VERIFY_TOKEN:
        return "Webhook verification is unavailable.", 503

    mode = request.args.get(
        "hub.mode"
    )

    token = request.args.get(
        "hub.verify_token"
    )

    challenge = request.args.get(
        "hub.challenge"
    )

    if (
        mode == "subscribe"
        and isinstance(token, str)
        and hmac.compare_digest(token, VERIFY_TOKEN)
    ):

        print(
            "Webhook verified successfully!"
        )

        return challenge, 200

    return "Verification failed", 403


# ==================================================
# JSON HELPERS
# ==================================================

# Durable legacy-bot state lives in Postgres when it is available (see
# database/legacy_state.py); the JSON files below are the fallback for
# local/dev runs without a database and for any database hiccup, so a DB
# failure can never break the webhook.
_LEGACY_STATE_STORE = None
_LEGACY_STATE_STORE_CHECKED_AT = None


def _legacy_state_store():
    """Return the Postgres-backed legacy state store, or None for JSON."""

    global _LEGACY_STATE_STORE, _LEGACY_STATE_STORE_CHECKED_AT
    if _LEGACY_STATE_STORE is not None:
        return _LEGACY_STATE_STORE
    now = datetime.now(timezone.utc)
    if (
        _LEGACY_STATE_STORE_CHECKED_AT is not None
        and (now - _LEGACY_STATE_STORE_CHECKED_AT).total_seconds() < 60
    ):
        return None
    _LEGACY_STATE_STORE_CHECKED_AT = now
    try:
        from database.legacy_state import legacy_state_store

        store = legacy_state_store(_oauth_session_factory(), INSTAGRAM_USER_ID)
    except Exception:
        return None
    if store is None:
        return None
    _LEGACY_STATE_STORE = store
    return store


def _legacy_state_error(context):
    print("LEGACY STATE DB ERROR", context)













































# In-memory cache for the /api/leads Instagram media fetch only (the endpoint
# serves the single legacy account). Local requests/pending reads are NOT
# cached. TTL: 60 seconds.
_LEADS_MEDIA_CACHE = {"fetched_at": 0.0, "media_map": {}}
_LEADS_MEDIA_CACHE_TTL_SECONDS = 60


@app.route("/api/leads", methods=["GET"])
def get_dashboard_leads():
    """Return recent prompt requests enriched with Instagram post details.

    A request is logged only after the private access request was sent. While
    the user is still present in pending_access.json the status is waiting for
    confirmation; once that pending record is cleared after the prompt button
    is delivered, the request is shown as delivered.
    """
    requests_data = load_requests()
    pending_data = load_json_file(PENDING_ACCESS_FILE, {})
    if not isinstance(pending_data, dict):
        pending_data = {}

    # Read recent Instagram media once so every lead can show the post that
    # generated it without making one API request per row. The media fetch
    # is cached for 60 seconds; local requests/pending reads run every load.
    media_map = {}
    if INSTAGRAM_ACCESS_TOKEN:
        now = time.time()
        if (
            _LEADS_MEDIA_CACHE["media_map"]
            and now - _LEADS_MEDIA_CACHE["fetched_at"] < _LEADS_MEDIA_CACHE_TTL_SECONDS
        ):
            media_map = _LEADS_MEDIA_CACHE["media_map"]
        else:
            try:
                media_response = requests.get(
                    f"https://graph.instagram.com/{API_VERSION}/{INSTAGRAM_USER_ID}/media",
                    params={
                        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
                        "limit": 50,
                        "access_token": INSTAGRAM_ACCESS_TOKEN
                    },
                    timeout=30
                )
                media_data = media_response.json()
                if media_response.ok and isinstance(media_data, dict):
                    for item in media_data.get("data", []):
                        if not isinstance(item, dict):
                            continue
                        media_id = str(item.get("id", "") or "")
                        if media_id:
                            media_map[media_id] = item
                    _LEADS_MEDIA_CACHE["media_map"] = media_map
                    _LEADS_MEDIA_CACHE["fetched_at"] = time.time()
            except Exception as error:
                print("LEADS MEDIA LOOKUP ERROR:", type(error).__name__)

    leads = []
    for item in reversed(requests_data[-200:]):
        if not isinstance(item, dict):
            continue

        media_id = str(item.get("media_id", "") or "")
        commenter_id = str(item.get("commenter_id", "") or "")
        pending = pending_data.get(commenter_id) if commenter_id else None
        media = media_map.get(media_id, {})

        if pending:
            status = "waiting_confirmation"
            status_label = "Access request sent"
        else:
            status = "delivered"
            status_label = "Prompt access delivered"

        leads.append({
            "request_key": str(item.get("request_key", "") or ""),
            "media_id": media_id,
            "comment_id": str(item.get("comment_id", "") or ""),
            "commenter_id": commenter_id,
            "username": str(item.get("username", "") or ""),
            "page_url": str(item.get("page_url", "") or ""),
            "created_at": str(item.get("created_at", "") or ""),
            "status": status,
            "status_label": status_label,
            "post_caption": str(media.get("caption", "") or ""),
            "post_media_type": str(media.get("media_type", "") or ""),
            "post_media_url": str(media.get("media_url", "") or ""),
            "post_thumbnail_url": str(media.get("thumbnail_url", "") or ""),
            "post_permalink": str(media.get("permalink", "") or ""),
            "post_timestamp": str(media.get("timestamp", "") or "")
        })

    waiting = sum(1 for item in leads if item["status"] == "waiting_confirmation")
    delivered = sum(1 for item in leads if item["status"] == "delivered")
    profiles = aggregate_lead_profiles(leads)
    returning_people = sum(1 for item in profiles if item.get("request_count", 0) >= 2)
    hot_leads = sum(1 for item in profiles if item.get("temperature") == "hot")

    followups = load_followups()
    latest_followup_by_person = {}
    for followup in followups:
        if not isinstance(followup, dict):
            continue
        recipient_id = str(followup.get("recipient_id", "") or "")
        if recipient_id:
            latest_followup_by_person[recipient_id] = followup

    for profile in profiles:
        recipient_id = str(profile.get("commenter_id", "") or "")
        followup = latest_followup_by_person.get(recipient_id, {})
        profile["followup_status"] = str(followup.get("status", "") or "")
        profile["followup_created_at"] = str(followup.get("created_at", "") or "")
        profile["followup_sent_at"] = str(followup.get("sent_at", "") or "")

    followup_pending = sum(1 for item in followups if item.get("status") == "pending")
    followup_sent = sum(1 for item in followups if item.get("status") == "sent")

    return {
        "leads": leads,
        "profiles": profiles,
        "summary": {
            "total": len(leads),
            "waiting": waiting,
            "delivered": delivered,
            "unique_people": len(profiles),
            "returning_people": returning_people,
            "hot_leads": hot_leads,
            "followup_pending": followup_pending,
            "followup_sent": followup_sent
        }
    }, 200




























# Prompt extraction lives in prompt_pages.py; re-exported here so
# existing callers and tests keep working against the webhook module.
from prompt_pages import (
    MEDIA_LOCKS,
    MEDIA_LOCKS_LOCK,
    download_image_bytes,
    extract_prompts_with_gemini,
    get_carousel_items,
    get_instagram_media,
    get_media_lock,
    parse_gemini_json,
)




# ==================================================
# POST PREVIEW IMAGE
# ==================================================

@app.route(
    "/preview/<media_id>",
    methods=["GET"]
)
def post_preview(
    media_id
):

    media_info = get_instagram_media(
        media_id
    )

    if not media_info:

        return Response(
            status=404
        )

    media_type = media_info.get(
        "media_type",
        ""
    )

    preview_url = ""

    # Normal image post
    if media_type == "IMAGE":

        preview_url = media_info.get(
            "media_url",
            ""
        )

    # Carousel: use the first image as preview
    elif media_type == "CAROUSEL_ALBUM":

        carousel_items = get_carousel_items(
            media_info
        )

        for item in carousel_items:

            if item.get(
                "media_type"
            ) == "IMAGE":

                preview_url = item.get(
                    "media_url",
                    ""
                )

                if preview_url:
                    break

    if not preview_url:

        return Response(
            status=404
        )

    image = download_image_bytes(
        preview_url
    )

    if not image:

        return Response(
            status=404
        )

    response = Response(
        image["data"],
        mimetype=image["mime_type"]
    )

    # Short cache so Instagram CDN URLs can refresh when needed
    response.headers["Cache-Control"] = (
        "public, max-age=300"
    )

    return response


# ==================================================
# PROMPT PAGE
# ==================================================

@app.route("/p/o/<public_id>", methods=["GET"])
def oauth_prompt_page(public_id):
    """Neutral, opaque OAuth prompt page; legacy /p/<media_id> stays untouched."""
    session_factory = _oauth_session_factory()
    if session_factory is None:
        return Response("Prompt page is unavailable.", status=404, mimetype="text/plain")
    try:
        with session_factory() as db_session:
            post = get_oauth_prompt_post_by_public_id(db_session, public_id)
            prompts = list(post.prompts) if post is not None and isinstance(post.prompts, list) else []
    except Exception:
        return Response("Prompt page is unavailable.", status=404, mimetype="text/plain")
    if post is None:
        return Response("Prompt page is unavailable.", status=404, mimetype="text/plain")
    if _oauth_prompt_cover_refresh_due(post):
        _oauth_refresh_prompt_post_metadata(
            session_factory,
            post.instagram_account_id,
            post.media_id,
            post.public_id,
        )
        try:
            with session_factory() as db_session:
                refreshed = get_oauth_prompt_post_by_public_id(db_session, public_id)
                if refreshed is not None:
                    post = refreshed
                    prompts = (
                        list(post.prompts) if isinstance(post.prompts, list) else []
                    )
        except Exception:
            pass
    cards = []
    prompt_ids = []
    for number, prompt in enumerate(prompts, start=1):
        text = prompt.get("text", "") if isinstance(prompt, dict) else str(prompt or "")
        text = str(text).strip()
        if text:
            prompt_id = f"oauth-prompt-{number}"
            prompt_ids.append(prompt_id)
            cards.append(f'''<article class="prompt-card"><div class="prompt-meta"><span>PROMPT {number}</span><button class="copy-button" type="button" onclick="copyPrompt('{prompt_id}',this)">Copy Prompt</button></div><div id="{prompt_id}" class="prompt-text">{html.escape(text)}</div></article>''')
    cover_url = str(post.cover_url or "").strip()
    permalink = str(post.permalink or "").strip()
    cover_html = ""
    if cover_url:
        cover_html = f'''<div class="post-preview" id="post-preview"><img src="{html.escape(cover_url, quote=True)}" alt="Instagram post preview" loading="eager" referrerpolicy="no-referrer" onerror="document.getElementById('post-preview').style.display='none'"></div>'''
    instagram_button = ""
    if permalink:
        instagram_button = f'''<a class="secondary-button" href="{html.escape(permalink, quote=True)}" target="_blank" rel="noopener noreferrer">View Instagram Post</a>'''
    prompt_ids_json = json.dumps(prompt_ids)
    cards_html = "".join(cards) or '<p class="empty">No prompts are available.</p>'
    return Response(
        f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b0b0d"><title>AI Prompts</title><style>
:root{{--bg:#0b0b0d;--panel:#151518;--panel2:#1b1b1f;--line:#2a2a30;--text:#f7f7f8;--muted:#a6a6af}}*{{box-sizing:border-box}}body{{margin:0;background:radial-gradient(circle at top,rgba(255,255,255,.06),transparent 28rem),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}}button,a{{-webkit-tap-highlight-color:transparent}}.container{{width:min(860px,100%);margin:auto;padding:20px 16px 70px}}.brand{{display:flex;justify-content:space-between;gap:16px;padding:6px 2px 20px;font-size:14px;font-weight:800;letter-spacing:.13em}}.badge{{color:var(--muted);font-size:12px;border:1px solid var(--line);border-radius:999px;padding:7px 10px;letter-spacing:normal}}.post-preview{{overflow:hidden;border:1px solid var(--line);border-radius:24px;background:var(--panel);margin-bottom:22px}}.post-preview img{{display:block;width:100%;max-height:720px;object-fit:contain;background:#0f0f12}}.eyebrow{{color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.16em}}h1{{margin:10px 0;font-size:clamp(34px,8vw,58px);letter-spacing:-.045em}}.description{{color:var(--muted);line-height:1.65}}.actions{{display:grid;gap:10px;margin:24px 0 26px}}.primary-button,.secondary-button,.copy-button{{border:0;border-radius:14px;font-weight:750;cursor:pointer}}.primary-button{{padding:15px 18px;background:#fff;color:#0c0c0e;font-size:15px}}.secondary-button{{padding:15px 18px;text-align:center;text-decoration:none;background:var(--panel2);color:var(--text);border:1px solid var(--line)}}.prompt-list{{display:grid;gap:14px}}.prompt-card{{overflow:hidden;background:rgba(21,21,24,.96);border:1px solid var(--line);border-radius:22px}}.prompt-meta{{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 14px 12px 18px;border-bottom:1px solid var(--line);color:var(--muted);font-size:12px;font-weight:850;letter-spacing:.12em}}.copy-button{{padding:9px 12px;background:#2a2a2f;color:var(--text);font-size:12px;letter-spacing:normal}}.prompt-text{{white-space:pre-wrap;overflow-wrap:anywhere;padding:19px 18px 22px;line-height:1.72;font-size:15px}}.empty{{color:var(--muted)}}.footer{{padding:30px 4px 0;color:#777782;text-align:center;font-size:12px}}.toast{{position:fixed;left:50%;bottom:max(22px,env(safe-area-inset-bottom));transform:translate(-50%,14px);opacity:0;pointer-events:none;background:#fff;color:#0c0c0e;border-radius:999px;padding:11px 16px;font-size:13px;font-weight:800;transition:.2s;z-index:999}}.toast.show{{transform:translate(-50%,0);opacity:1}}@media(min-width:680px){{.container{{padding:30px 24px 90px}}.actions{{grid-template-columns:1fr 1fr}}.prompt-text{{padding:22px 22px 26px;font-size:16px}}}}</style></head><body><main class="container"><div class="brand"><div>AI PROMPTS</div><div class="badge">Prompt Library</div></div>{cover_html}<header><p class="eyebrow">READY TO COPY</p><h1>AI Prompts</h1><p class="description">Copy one prompt or copy everything at once.</p></header><div class="actions"><button class="primary-button" type="button" onclick="copyAllPrompts()">Copy All Prompts</button>{instagram_button}</div><section class="prompt-list">{cards_html}</section><footer class="footer">Generated automatically</footer></main><div id="toast" class="toast">Prompt copied ✓</div><script>
var promptIds={prompt_ids_json};function toast(m){{var t=document.getElementById('toast');t.innerText=m;t.className='toast show';clearTimeout(window.__toastTimer);window.__toastTimer=setTimeout(function(){{t.className='toast'}},1600)}}function fallbackCopyText(text){{var a=document.createElement('textarea');a.value=text;a.setAttribute('readonly','readonly');a.style.cssText='position:fixed;top:0;left:0;width:1px;height:1px;opacity:.01';document.body.appendChild(a);a.focus();a.select();try{{a.setSelectionRange(0,a.value.length)}}catch(e){{}}var ok=false;try{{ok=document.execCommand('copy')}}catch(e){{}}document.body.removeChild(a);return ok}}function manualCopy(text){{var a=document.createElement('textarea');a.value=text;a.readOnly=true;a.style.cssText='position:fixed;inset:10%;width:80%;height:70%;z-index:99999;padding:16px;font-size:16px';document.body.appendChild(a);a.focus();a.select();toast('Select and copy manually')}}function copyText(text,done){{try{{if(navigator.clipboard&&navigator.clipboard.writeText){{navigator.clipboard.writeText(text).then(function(){{done(true)}},function(){{done(fallbackCopyText(text))}});return}}}}catch(e){{}}done(fallbackCopyText(text))}}function copyPrompt(id,button){{var e=document.getElementById(id),text=e&&(e.innerText||e.textContent||'').trim();if(!text){{toast('Prompt not found');return}}var original=button.innerText;copyText(text,function(ok){{if(ok){{button.innerText='Copied ✓';toast('Prompt copied ✓');setTimeout(function(){{button.innerText=original}},1600)}}else{{manualCopy(text)}}}})}}function copyAllPrompts(){{var parts=[];for(var i=0;i<promptIds.length;i++){{var e=document.getElementById(promptIds[i]),text=e&&(e.innerText||e.textContent||'').trim();if(text)parts.push('PROMPT '+(i+1)+'\\n\\n'+text)}}var all=parts.join('\\n\\n--------------------\\n\\n');if(!all){{toast('No prompts found');return}}copyText(all,function(ok){{if(ok)toast('All prompts copied ✓');else manualCopy(all)}})}}document.documentElement.setAttribute('data-copy-script','ready');</script></body></html>''',
        mimetype="text/html",
    )


@app.route(
    "/p/<media_id>",
    methods=["GET"]
)
def prompt_page(
    media_id
):

    post = get_saved_post(
        media_id
    )

    if not post:

        return Response(
            """
            <!doctype html>
            <html lang="en">
            <head>
                <meta charset="utf-8">
                <meta name="viewport" content="width=device-width,initial-scale=1">
                <title>Prompt not found</title>
                <style>
                    body {
                        margin: 0;
                        background: #0b0b0d;
                        color: #ffffff;
                        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
                        display: grid;
                        place-items: center;
                        min-height: 100vh;
                        padding: 24px;
                    }
                    .empty {
                        width: min(520px, 100%);
                        text-align: center;
                        background: #151518;
                        border: 1px solid #27272c;
                        border-radius: 24px;
                        padding: 34px 24px;
                    }
                    h1 { margin: 0 0 10px; font-size: 28px; }
                    p { margin: 0; color: #aaaab2; line-height: 1.6; }
                </style>
            </head>
            <body>
                <div class="empty">
                    <h1>Prompts not found</h1>
                    <p>This prompt page is not available yet.</p>
                </div>
            </body>
            </html>
            """,
            status=404,
            mimetype="text/html"
        )

    prompts = post.get(
        "prompts",
        []
    )

    permalink = post.get(
        "permalink",
        ""
    )

    cover_url = post.get(
        "cover_url",
        ""
    )

    cards = []
    all_prompt_ids = []

    for number, prompt in enumerate(
        prompts,
        start=1
    ):

        prompt_text = html.escape(
            prompt.get(
                "text",
                ""
            )
        )

        prompt_id = f"prompt-{number}"
        all_prompt_ids.append(prompt_id)

        cards.append(
            f"""
            <article class="prompt-card">

                <div class="prompt-meta">
                    <span class="prompt-number">PROMPT {number}</span>
                    <button
                        class="copy-button"
                        onclick="copyPrompt('{prompt_id}', this)"
                        type="button"
                    >
                        Copy Prompt
                    </button>
                </div>

                <div
                    id="{prompt_id}"
                    class="prompt-text"
                >{prompt_text}</div>

            </article>
            """
        )

    instagram_button = ""

    if permalink:

        safe_permalink = html.escape(
            permalink,
            quote=True
        )

        instagram_button = f"""
        <a
            class="secondary-button"
            href="{safe_permalink}"
            target="_blank"
            rel="noopener noreferrer"
        >
            View Instagram Post
        </a>
        """

    # Always use our own preview endpoint.
    # This also works for posts that were already saved
    # before cover_url was added to posts.json.
    preview_src = f"/preview/{html.escape(str(media_id), quote=True)}"

    cover_html = f"""
    <div class="post-preview" id="post-preview">
        <img
            src="{preview_src}"
            alt="Instagram post preview"
            loading="eager"
            onerror="document.getElementById('post-preview').style.display='none'"
        >
    </div>
    """

    prompt_ids_json = json.dumps(
        all_prompt_ids
    )

    page = f"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0b0d">
<title>Postenza — AI Prompt Library</title>

<style>
:root {{
    --bg: #0b0b0d;
    --panel: #151518;
    --panel-2: #1b1b1f;
    --line: #2a2a30;
    --text: #f7f7f8;
    --muted: #a6a6af;
    --soft: #777782;
    --accent: #ffffff;
}}

* {{
    box-sizing: border-box;
}}

html {{
    scroll-behavior: smooth;
}}

body {{
    margin: 0;
    background:
        radial-gradient(circle at top, rgba(255,255,255,.06), transparent 28rem),
        var(--bg);
    color: var(--text);
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
}}

button,
a {{
    -webkit-tap-highlight-color: transparent;
}}

.container {{
    width: min(860px, 100%);
    margin: 0 auto;
    padding: 20px 16px 70px;
}}

.brand {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 6px 2px 20px;
}}

.brand-name {{
    font-size: 14px;
    font-weight: 800;
    letter-spacing: .13em;
}}

.brand-badge {{
    color: var(--muted);
    font-size: 12px;
    border: 1px solid var(--line);
    background: rgba(255,255,255,.03);
    border-radius: 999px;
    padding: 7px 10px;
}}

.hero {{
    margin-bottom: 18px;
}}

.post-preview {{
    width: 100%;
    overflow: hidden;
    border-radius: 24px;
    border: 1px solid var(--line);
    background: var(--panel);
    margin-bottom: 22px;
    box-shadow: 0 20px 60px rgba(0,0,0,.24);
}}

.post-preview img {{
    display: block;
    width: 100%;
    max-height: 720px;
    object-fit: contain;
    background: #0f0f12;
}}

.eyebrow {{
    margin: 0 0 10px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .16em;
    text-transform: uppercase;
}}

.hero h1 {{
    margin: 0;
    font-size: clamp(34px, 8vw, 58px);
    line-height: .98;
    letter-spacing: -.045em;
}}

.hero-description {{
    margin: 15px 0 0;
    max-width: 620px;
    color: var(--muted);
    line-height: 1.65;
    font-size: 15px;
}}

.actions {{
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
    margin: 24px 0 26px;
}}

.primary-button,
.secondary-button,
.copy-button {{
    border: 0;
    border-radius: 14px;
    font-weight: 750;
    cursor: pointer;
    transition: transform .15s ease, opacity .15s ease, background .15s ease;
}}

.primary-button:active,
.secondary-button:active,
.copy-button:active {{
    transform: scale(.985);
}}

.primary-button {{
    width: 100%;
    padding: 15px 18px;
    background: var(--accent);
    color: #0c0c0e;
    font-size: 15px;
}}

.secondary-button {{
    display: block;
    width: 100%;
    padding: 15px 18px;
    text-align: center;
    text-decoration: none;
    background: var(--panel-2);
    color: var(--text);
    border: 1px solid var(--line);
    font-size: 15px;
}}

.prompt-list {{
    display: grid;
    gap: 14px;
}}

.prompt-card {{
    overflow: hidden;
    background: rgba(21,21,24,.96);
    border: 1px solid var(--line);
    border-radius: 22px;
    box-shadow: 0 12px 34px rgba(0,0,0,.14);
}}

.prompt-meta {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 14px 14px 12px 18px;
    border-bottom: 1px solid var(--line);
}}

.prompt-number {{
    color: var(--muted);
    font-size: 12px;
    font-weight: 850;
    letter-spacing: .12em;
}}

.copy-button {{
    padding: 9px 12px;
    background: #2a2a2f;
    color: var(--text);
    font-size: 12px;
}}

.copy-button:hover {{
    background: #34343a;
}}

.prompt-text {{
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    padding: 19px 18px 22px;
    color: #efeff2;
    line-height: 1.72;
    font-size: 15px;
}}

.footer {{
    padding: 30px 4px 0;
    color: var(--soft);
    text-align: center;
    font-size: 12px;
}}

.toast {{
    position: fixed;
    left: 50%;
    bottom: max(22px, env(safe-area-inset-bottom));
    transform: translate(-50%, 14px);
    opacity: 0;
    pointer-events: none;
    background: #ffffff;
    color: #0c0c0e;
    border-radius: 999px;
    padding: 11px 16px;
    font-size: 13px;
    font-weight: 800;
    box-shadow: 0 12px 40px rgba(0,0,0,.35);
    transition: all .2s ease;
    z-index: 999;
}}

.toast.show {{
    transform: translate(-50%, 0);
    opacity: 1;
}}

@media (min-width: 680px) {{
    .container {{
        padding: 30px 24px 90px;
    }}

    .actions {{
        grid-template-columns: 1fr 1fr;
    }}

    .prompt-text {{
        padding: 22px 22px 26px;
        font-size: 16px;
    }}

    .prompt-meta {{
        padding: 16px 16px 14px 22px;
    }}
}}
</style>
</head>

<body>

<main class="container">

    <div class="brand">
        <div class="brand-name">Postenza</div>
        <div class="brand-badge">AI Prompt Library</div>
    </div>

    {cover_html}

    <header class="hero">
        <p class="eyebrow">Ready to copy</p>
        <h1>AI Prompts</h1>
        <p class="hero-description">
            {len(prompts)} prompt(s) extracted automatically from this Instagram post.
            Copy one prompt or copy everything at once.
        </p>
    </header>

    <div class="actions">
        <button
            class="primary-button"
            type="button"
            onclick="copyAllPrompts()"
        >
            Copy All Prompts
        </button>

        {instagram_button}
    </div>

    <section class="prompt-list">
        {''.join(cards)}
    </section>

    <footer class="footer">
        Generated automatically by Postenza
    </footer>

</main>

<div id="toast" class="toast">Copied ✓</div>

<script>
var promptIds = {prompt_ids_json};

function showToast(message) {{
    var toast = document.getElementById("toast");
    if (!toast) return;
    toast.innerText = message;
    toast.className = "toast show";

    if (window.__toastTimer) {{
        clearTimeout(window.__toastTimer);
    }}

    window.__toastTimer = setTimeout(function() {{
        toast.className = "toast";
    }}, 1600);
}}

function fallbackCopyText(text) {{
    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "readonly");
    textarea.style.position = "fixed";
    textarea.style.top = "0";
    textarea.style.left = "0";
    textarea.style.width = "1px";
    textarea.style.height = "1px";
    textarea.style.padding = "0";
    textarea.style.border = "0";
    textarea.style.opacity = "0.01";
    textarea.style.fontSize = "16px";
    textarea.style.zIndex = "99999";

    document.body.appendChild(textarea);

    textarea.focus();
    textarea.select();

    try {{
        textarea.setSelectionRange(0, textarea.value.length);
    }} catch (e) {{}}

    var ok = false;

    try {{
        ok = document.execCommand("copy");
    }} catch (e) {{
        ok = false;
    }}

    document.body.removeChild(textarea);
    return ok;
}}

function copyText(text, onDone) {{
    // Most compatible path first for Instagram/Facebook in-app browsers.
    if (fallbackCopyText(text)) {{
        onDone(true);
        return;
    }}

    // Modern browser fallback.
    try {{
        if (
            navigator.clipboard &&
            typeof navigator.clipboard.writeText === "function"
        ) {{
            navigator.clipboard.writeText(text).then(
                function() {{
                    onDone(true);
                }},
                function() {{
                    onDone(false);
                }}
            );
            return;
        }}
    }} catch (e) {{}}

    onDone(false);
}}

function showManualCopy(text) {{
    var old = document.getElementById("copy-fallback-modal");
    if (old) old.parentNode.removeChild(old);

    var overlay = document.createElement("div");
    overlay.id = "copy-fallback-modal";
    overlay.style.position = "fixed";
    overlay.style.inset = "0";
    overlay.style.background = "rgba(0,0,0,.78)";
    overlay.style.zIndex = "999999";
    overlay.style.padding = "18px";
    overlay.style.display = "flex";
    overlay.style.alignItems = "center";
    overlay.style.justifyContent = "center";

    var box = document.createElement("div");
    box.style.width = "min(680px,100%)";
    box.style.maxHeight = "82vh";
    box.style.background = "#17171b";
    box.style.border = "1px solid #303036";
    box.style.borderRadius = "18px";
    box.style.padding = "16px";
    box.style.display = "flex";
    box.style.flexDirection = "column";
    box.style.gap = "12px";

    var title = document.createElement("div");
    title.innerText = "Copy prompt";
    title.style.fontWeight = "800";
    title.style.fontSize = "18px";
    title.style.color = "white";

    var area = document.createElement("textarea");
    area.value = text;
    area.readOnly = true;
    area.style.width = "100%";
    area.style.minHeight = "260px";
    area.style.maxHeight = "55vh";
    area.style.resize = "vertical";
    area.style.background = "#0f0f12";
    area.style.color = "white";
    area.style.border = "1px solid #35353c";
    area.style.borderRadius = "12px";
    area.style.padding = "14px";
    area.style.fontSize = "16px";
    area.style.lineHeight = "1.5";

    var close = document.createElement("button");
    close.type = "button";
    close.innerText = "Close";
    close.style.width = "100%";
    close.style.padding = "13px";
    close.style.borderRadius = "12px";
    close.style.border = "0";
    close.style.fontWeight = "800";
    close.onclick = function() {{
        if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
    }};

    box.appendChild(title);
    box.appendChild(area);
    box.appendChild(close);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    setTimeout(function() {{
        area.focus();
        area.select();
        try {{
            area.setSelectionRange(0, area.value.length);
        }} catch (e) {{}}
    }}, 50);
}}

function copyPrompt(id, button) {{
    var element = document.getElementById(id);

    if (!element) {{
        showToast("Prompt not found");
        return false;
    }}

    var text = (element.innerText || element.textContent || "").trim();

    if (!text) {{
        showToast("Prompt is empty");
        return false;
    }}

    var original = button ? button.innerText : "Copy Prompt";

    copyText(text, function(ok) {{
        if (ok) {{
            if (button) button.innerText = "Copied ✓";
            showToast("Prompt copied ✓");

            setTimeout(function() {{
                if (button) button.innerText = original;
            }}, 1600);
        }} else {{
            showManualCopy(text);
        }}
    }});

    return false;
}}

function copyAllPrompts() {{
    var parts = [];
    var i;

    for (i = 0; i < promptIds.length; i++) {{
        var element = document.getElementById(promptIds[i]);

        if (element) {{
            var text = (element.innerText || element.textContent || "").trim();

            if (text) {{
                parts.push("PROMPT " + (i + 1) + "\\n\\n" + text);
            }}
        }}
    }}

    if (!parts.length) {{
        showToast("No prompts found");
        return false;
    }}

    var allText = parts.join("\\n\\n--------------------\\n\\n");

    copyText(allText, function(ok) {{
        if (ok) {{
            showToast("All prompts copied ✓");
        }} else {{
            showManualCopy(allText);
        }}
    }});

    return false;
}}

// Confirm the script has actually loaded.
document.documentElement.setAttribute("data-copy-script", "ready");
</script>

</body>
</html>
"""

    return Response(
        page,
        mimetype="text/html"
    )


















# ==================================================
# BACKGROUND PROCESSOR
# ==================================================

WEBHOOK_EXTERNAL_ID_PATTERN = re.compile(r"^[0-9]{1,64}$")


def _webhook_entry_external_id(entry):
    """Return a canonical, numeric envelope ID without accepting fallbacks."""

    if not isinstance(entry, dict):
        return None
    external_id = entry.get("id")
    if not isinstance(external_id, str):
        return None
    external_id = external_id.strip()
    return external_id if WEBHOOK_EXTERNAL_ID_PATTERN.fullmatch(external_id) else None


def _resolve_webhook_entry_route(entry_external_id):
    """Resolve one entry through a fresh database session, without credentials."""

    auth_state = app.extensions.get("phase3a_auth")
    session_factory = getattr(auth_state, "session_factory", None)
    if session_factory is None:
        raise RuntimeAccountRouteUnavailableError("Instagram account route is unavailable.")
    with session_factory() as db_session:
        return resolve_instagram_account_route(
            db_session,
            instagram_user_id=entry_external_id,
            legacy_instagram_user_id=INSTAGRAM_USER_ID,
        )


def _messaging_event_matches_entry(event, entry_external_id):
    """Require a supplied messaging recipient to agree with its envelope ID."""

    if not isinstance(event, dict):
        return False
    if "recipient" not in event:
        return True
    recipient = event.get("recipient")
    if not isinstance(recipient, dict):
        return False
    recipient_id = recipient.get("id")
    if not isinstance(recipient_id, str):
        return False
    recipient_id = recipient_id.strip()
    return bool(
        WEBHOOK_EXTERNAL_ID_PATTERN.fullmatch(recipient_id)
        and hmac.compare_digest(recipient_id, entry_external_id)
    )


def _process_legacy_webhook_entry(entry, entry_external_id, public_base_url):
    """Invoke unchanged legacy handlers only after exact route classification."""

    changes = entry.get("changes", [])
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            if change.get("field") != "comments":
                continue
            value = change.get("value", {})
            if isinstance(value, dict):
                process_comment(value, public_base_url)

    messaging_events = entry.get("messaging", [])
    if isinstance(messaging_events, list):
        for event in messaging_events:
            if _messaging_event_matches_entry(event, entry_external_id):
                process_message_event(event)


def persist_oauth_webhook_ingress(data, public_base_url):
    """Queue normalized OAuth work before acknowledgement; legacy work stays untouched."""
    session_factory = _oauth_session_factory()
    entries = data.get("entry", []) if isinstance(data, dict) else []
    if session_factory is None or not isinstance(entries, list):
        return 0
    persisted = 0
    for entry in entries:
        entry_external_id = _webhook_entry_external_id(entry)
        if entry_external_id is None:
            continue
        try:
            route = _resolve_webhook_entry_route(entry_external_id)
        except Exception:
            # Preserve the existing fail-closed route behavior: an unknown or
            # currently unavailable account is not allowed into a legacy path.
            continue
        if route.connection_mode != "oauth":
            continue
        changes = entry.get("changes", []) if isinstance(entry, dict) else []
        messaging_events = entry.get("messaging", []) if isinstance(entry, dict) else []
        with session_factory.begin() as db_session:
            if isinstance(changes, list):
                for change in changes:
                    if not isinstance(change, dict) or change.get("field") != "comments":
                        continue
                    if enqueue_oauth_webhook_work(
                        db_session,
                        instagram_account_id=route.instagram_account_id,
                        instagram_user_id=entry_external_id,
                        event_type="comment",
                        value=change.get("value"),
                        public_base_url=public_base_url,
                    ):
                        persisted += 1
            if isinstance(messaging_events, list):
                for event in messaging_events:
                    if not _messaging_event_matches_entry(event, entry_external_id):
                        continue
                    if enqueue_oauth_webhook_work(
                        db_session,
                        instagram_account_id=route.instagram_account_id,
                        instagram_user_id=entry_external_id,
                        event_type="message",
                        value=event,
                        public_base_url=public_base_url,
                    ):
                        persisted += 1
    return persisted


def _oauth_session_factory():
    """Return the configured database factory without a legacy fallback."""
    auth_state = app.extensions.get("phase3a_auth")
    return getattr(auth_state, "session_factory", None)

configure_legacy_state_session_factory(_oauth_session_factory)


def _resolve_account_gemini_key(session_factory, account_id):
    """Return the account owner's BYOK Gemini key, or None for the shared key."""

    try:
        from database.gemini_keys import resolve_account_gemini_key

        return resolve_account_gemini_key(session_factory, account_id)
    except Exception:
        return None


def _oauth_words(value):
    return re.findall(r"\b\w+\b", str(value or "").lower())


def generate_oauth_smart_reply(comment_text, *, tone="friendly", api_key=None):
    """Return the ready generic reply for a non-keyword comment (v2).

    Postenza v2 policy: no Gemini smart replies. The reply is always the
    shared ready generic text (see ``comment_pipeline.generic_ready_reply``)
    so a reply goes out even with no Gemini key, no client, or a failed
    provider call. ``tone``/``api_key`` are kept for call compatibility
    only and are intentionally ignored; ``comment_text`` is unused.
    """

    return generic_ready_reply()


def generate_oauth_lead_intelligence(context, *, api_key=None):
    """Lead analysis is disabled in Postenza v2 — never call Gemini.

    Kept as a no-op so the ``lead_intelligence_analyzer`` extension slot
    still exists; it always returns None and the tenant dashboard route
    answers 410. ``context``/``api_key`` are accepted and ignored.
    """

    print("LEAD INTELLIGENCE: analysis is disabled in Postenza v2.")
    return None


app.extensions["lead_intelligence_analyzer"] = generate_oauth_lead_intelligence


def _oauth_smart_reply_eligible(comment_text):
    """Exclude empty, emoji-only, and too-short public comments."""

    source_text = str(comment_text or "").strip()
    return len(source_text) >= 3 and bool(_oauth_words(source_text))


def extract_oauth_prompts(media, *, api_key=None):
    """Extract only visible prompts with a generic, account-neutral Gemini prompt."""
    if (GEMINI_CLIENT is None and not api_key) or not isinstance(media, dict):
        return []
    urls = []
    for item in [media, *((media.get("children") or {}).get("data") or [])]:
        if not isinstance(item, dict):
            continue
        url = item.get("media_url") or item.get("thumbnail_url")
        if isinstance(url, str) and url.startswith("https://"):
            urls.append(url)
    if not urls:
        return []
    contents = ["""Extract only complete AI image or video generation prompts visibly written in these Instagram images. Ignore usernames, watermarks, captions, UI labels, calls to action, and decorative text. Do not invent text. Return JSON only: {\"prompts\":[{\"source_index\":1,\"text\":\"...\"}]}."""]
    try:
        for url in urls[:10]:
            response = requests.get(url, timeout=15)
            if response.ok and response.content:
                contents.append(types.Part.from_bytes(data=response.content, mime_type=response.headers.get("Content-Type", "image/jpeg")))
        if len(contents) == 1:
            return []
        response = _gemini_generate_content(
            contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
            api_key=api_key,
        )
        return parse_gemini_json(str(response.text or ""))
    except Exception:
        print("OAUTH ENGAGEMENT: prompt extraction unavailable.")
        return []


def _oauth_complete(session_factory, account_id, provider_event_id, event_type, *, failed=False):
    with session_factory.begin() as db_session:
        complete_oauth_event(db_session, account_id, provider_event_id, event_type, failed=failed)


def _oauth_provider_https_url(value):
    """Return a provider media URL only when it is safe for public image markup."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        parsed.port
    except (TypeError, ValueError):
        return ""
    return candidate


def _oauth_prompt_cover_url(media):
    """Select a usable image cover from provider-returned media metadata."""
    if not isinstance(media, dict):
        return ""

    media_type = str(media.get("media_type") or "").strip().upper()
    if media_type == "IMAGE":
        return _oauth_provider_https_url(media.get("media_url"))
    if media_type in {"VIDEO", "REEL", "REELS"}:
        return _oauth_provider_https_url(media.get("thumbnail_url"))
    if media_type == "CAROUSEL_ALBUM":
        cover_url = _oauth_provider_https_url(media.get("media_url"))
        if cover_url:
            return cover_url

    children = media.get("children")
    child_items = children.get("data") if isinstance(children, dict) else []
    if not isinstance(child_items, list):
        return ""
    for child in child_items:
        if not isinstance(child, dict):
            continue
        child_type = str(child.get("media_type") or "").strip().upper()
        if child_type == "IMAGE":
            cover_url = _oauth_provider_https_url(child.get("media_url"))
        elif child_type in {"VIDEO", "REEL", "REELS"}:
            cover_url = _oauth_provider_https_url(child.get("thumbnail_url"))
        else:
            continue
        if cover_url:
            return cover_url
    return ""


def _oauth_utc_timestamp(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _oauth_prompt_cover_refresh_due(post, *, now=None):
    """Return whether cached provider cover metadata is due for refresh.

    ``updated_at`` doubles as the last refresh-attempt timestamp in this
    migration-free cache. A missing cover gets one immediate attempt when the
    row has never been updated, then observes the same TTL after that attempt.
    """
    current_time = _oauth_utc_timestamp(now) or datetime.now(timezone.utc)
    updated_at = _oauth_utc_timestamp(getattr(post, "updated_at", None))
    if updated_at is None or updated_at <= current_time - OAUTH_PROMPT_COVER_METADATA_TTL:
        return True
    if str(getattr(post, "cover_url", "") or "").strip():
        return False
    created_at = _oauth_utc_timestamp(getattr(post, "created_at", None))
    return created_at is None or updated_at <= created_at


def _oauth_refresh_prompt_post_metadata(
    session_factory, account_id, media_id, public_id, *, now=None
):
    """Best-effort account-scoped refresh of cached prompt presentation data."""
    current_time = _oauth_utc_timestamp(now) or datetime.now(timezone.utc)
    try:
        with session_factory.begin() as db_session:
            post = get_oauth_prompt_post(db_session, account_id, media_id)
            if post is None or str(post.public_id) != str(public_id):
                return False
            if not _oauth_prompt_cover_refresh_due(post, now=current_time):
                return True
            external_id = oauth_followup_account_external_id(db_session, account_id)
            # ``updated_at`` is also the attempt timestamp so a failed provider
            # lookup cannot be retriggered by every public page request.
            post.updated_at = current_time
    except Exception:
        return False

    if not external_id:
        return True
    try:
        media = lookup_oauth_media(
            session_factory,
            instagram_account_id=account_id,
            instagram_user_id=external_id,
            media_id=media_id,
        )
    except Exception:
        return True

    cover_url = _oauth_prompt_cover_url(media)
    permalink = str(media.get("permalink") or "")
    try:
        with session_factory.begin() as db_session:
            post = get_oauth_prompt_post(db_session, account_id, media_id)
            if post is None or str(post.public_id) != str(public_id):
                return False
            if cover_url:
                post.cover_url = cover_url
            if not str(post.permalink or "").strip() and permalink:
                post.permalink = permalink
        return True
    except Exception:
        return False


def _oauth_prompt_post_for_comment(session_factory, account_id, external_id, media_id, *, api_key=None):
    cached_public_id = None
    refresh_cached_metadata = False
    with session_factory() as db_session:
        cached = get_oauth_prompt_post(db_session, account_id, media_id)
        if cached is not None:
            cached_public_id = cached.public_id
            refresh_cached_metadata = _oauth_prompt_cover_refresh_due(cached)
    if cached_public_id is not None:
        if refresh_cached_metadata:
            _oauth_refresh_prompt_post_metadata(
                session_factory,
                account_id,
                media_id,
                cached_public_id,
            )
        return cached_public_id
    media = lookup_oauth_media(
        session_factory,
        instagram_account_id=account_id,
        instagram_user_id=external_id,
        media_id=media_id,
    )
    cover_url = _oauth_prompt_cover_url(media)
    permalink = str(media.get("permalink") or "")
    if GEMINI_CLIENT is not None or api_key:
        from database.billing import reserve_account_ai
        reserve_account_ai(session_factory, account_id)
    prompts = extract_oauth_prompts(media, api_key=api_key)
    if not prompts:
        return None
    with session_factory.begin() as db_session:
        record = oauth_prompt_post(
            db_session,
            account_id,
            media_id,
            prompts,
            permalink=permalink,
            cover_url=cover_url,
        )
        return record.public_id


def _log_oauth_engagement_failure(exc, *, context):
    """Print a credential-safe diagnostic for a swallowed OAuth engagement failure.

    The original code used a bare ``except Exception:`` that printed a generic
    "processing unavailable" message, hiding the real failing condition (e.g.
    capability gate, credential resolution, provider rejection).  This helper
    surfaces the exception type and a sanitized message so operators can
    diagnose the exact blocker without exposing tokens, secrets, or raw
    provider payloads.
    """

    exc_type = type(exc).__name__
    raw_message = str(exc) or ""
    # Never print raw exception text that might contain a token, URL, or
    # provider payload fragment.  Only surface known-safe sentinel messages.
    safe_messages = {
        "oauth comment delivery unavailable",
        "oauth prompt media unavailable",
        "oauth prompt extraction unavailable",
        "oauth public reply unavailable",
        "oauth private reply unavailable",
        "oauth prompt access unavailable",
        "Instagram engagement is temporarily unavailable.",
    }
    safe_message = raw_message if raw_message in safe_messages else "sanitized"
    print(
        f"OAUTH ENGAGEMENT: {context} processing unavailable. "
        f"reason={exc_type}:{safe_message}"
    )


def _log_oauth_followup_claim_failure(exc):
    """Report a safe class-only diagnostic for scheduler claim failures."""

    print(
        "OAUTH FOLLOW-UP: job claim unavailable. "
        f"reason={type(exc).__name__}:sanitized"
    )


def _oauth_prompt_extraction_fallback(session_factory, route, entry_external_id, automation_config, comment_id, commenter_id):
    """Send the ready DM/Link message when prompt extraction fails (v2).

    Prompt mode promised a prompt page; when extraction produces nothing
    the comment must NOT go silent (golden rule: replies never stop).
    Fall back to the account's ready direct-message text — link mode
    appends its destination URL — with the configured public reply first
    when enabled. Returns True when the private reply was sent.
    """

    if automation_config.public_reply_enabled:
        if not send_oauth_public_comment_reply(
            session_factory,
            instagram_account_id=route.instagram_account_id,
            instagram_user_id=entry_external_id,
            comment_id=comment_id,
            message=automation_config.public_reply_text,
        ):
            raise RuntimeError("oauth public reply unavailable")
    private_message_text = str(
        getattr(automation_config, "private_message_text", "") or ""
    ).strip()
    if automation_config.delivery_mode == "link":
        destination_url = str(
            getattr(automation_config, "destination_url", "") or ""
        ).strip()
        if destination_url:
            private_message_text = (
                f"{private_message_text}\n\n{destination_url}"
                if private_message_text
                else destination_url
            )
    if not private_message_text:
        private_message_text = DEFAULT_CUSTOM_MESSAGE
    recipient_id = send_oauth_private_comment_reply(
        session_factory,
        instagram_account_id=route.instagram_account_id,
        instagram_user_id=entry_external_id,
        comment_id=comment_id,
        message=private_message_text,
        recipient_id_fallback=commenter_id,
    )
    if not recipient_id:
        return False
    with session_factory.begin() as db_session:
        record_oauth_direct_message_interaction(
            db_session,
            route.instagram_account_id,
            comment_id,
            recipient_id,
        )
        complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
    print("OAUTH FOLLOW-UP: waiting_for_recipient_reply")
    return True


def _process_oauth_comment(value, route, entry_external_id, public_base_url, session_factory):
    # Shared pipeline decisions (see comment_pipeline.py): parse, own
    # comment guard, trigger-word check. The stage order below is the
    # same as process_comment's.
    from comment_pipeline import (
        comment_has_trigger_word,
        is_own_account_comment,
        parse_comment_event,
    )

    parsed = parse_comment_event(value)
    comment_id = parsed["comment_id"]
    comment_text = parsed["comment_text"]
    commenter_id = parsed["commenter_id"]
    username = parsed["username"]
    media_id = parsed["media_id"]
    if (
        not comment_id
        or not comment_text
        or not commenter_id
        or is_own_account_comment(
            commenter_id, username, owner_id=entry_external_id
        )
    ):
        return
    with session_factory() as db_session:
        automation_config = oauth_automation_runtime_config(
            db_session,
            instagram_account_id=route.instagram_account_id,
        )
    with session_factory.begin() as db_session:
        if not claim_oauth_event(db_session, route.instagram_account_id, comment_id, "comment"):
            return
    # BYOK: resolve the account owner's own Gemini key once; None means the
    # shared env key is used (and the legacy account always uses it).
    account_api_key = _resolve_account_gemini_key(
        session_factory, route.instagram_account_id
    )
    if automation_config is None:
        is_keyword_match = False
        delivery_mode = "prompt_request"
        prompt_request_decision = "runtime_config_unavailable"
    else:
        is_keyword_match = automation_config.enabled and comment_has_trigger_word(
            comment_text, automation_config.trigger_keywords
        )
        delivery_mode = getattr(automation_config, "delivery_mode", "prompt_request")
        prompt_request_decision = "keyword_no_match"
        if is_keyword_match:
            prompt_request_decision = {
                "direct_message": "direct_message_matched",
                "link": "link_matched",
            }.get(delivery_mode, "prompt_request_matched")
    print(f"OAUTH AUTOMATION: prompt_request_decision={prompt_request_decision}")
    try:
        if not is_keyword_match:
            # v2: every non-keyword comment gets the ready generic reply.
            # The only gates are the master automation switch, the
            # eligibility check (empty/emoji-only), and the hourly cap —
            # never Gemini availability. Replies must never stop.
            if (
                automation_config is None
                or not automation_config.enabled
                or not _oauth_smart_reply_eligible(comment_text)
            ):
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            with session_factory() as db_session:
                allowed = oauth_smart_reply_allowed(
                    db_session,
                    route.instagram_account_id,
                    cutoff=cutoff,
                    limit=automation_config.smart_reply_max_per_hour,
                )
            if not allowed:
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            reply = generate_oauth_smart_reply(
                comment_text,
                tone=automation_config.smart_reply_tone,
                api_key=account_api_key,
            )
            if not reply:
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            if not send_oauth_public_comment_reply(session_factory, instagram_account_id=route.instagram_account_id, instagram_user_id=entry_external_id, comment_id=comment_id, message=reply):
                raise RuntimeError("oauth comment delivery unavailable")
            with session_factory.begin() as db_session:
                record_oauth_smart_reply(db_session, route.instagram_account_id, comment_id, commenter_id, username, reply)
                complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
            return

        if delivery_mode in {"direct_message", "link"}:
            if automation_config.public_reply_enabled:
                if not send_oauth_public_comment_reply(
                    session_factory,
                    instagram_account_id=route.instagram_account_id,
                    instagram_user_id=entry_external_id,
                    comment_id=comment_id,
                    message=automation_config.public_reply_text,
                ):
                    raise RuntimeError("oauth public reply unavailable")
            private_message_text = str(
                getattr(automation_config, "private_message_text", "") or ""
            ).strip()
            if delivery_mode == "link":
                destination_url = str(
                    getattr(automation_config, "destination_url", "") or ""
                ).strip()
                private_message_text = f"{private_message_text}\n\n{destination_url}"
            if not private_message_text:
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            recipient_id = send_oauth_private_comment_reply(
                session_factory,
                instagram_account_id=route.instagram_account_id,
                instagram_user_id=entry_external_id,
                comment_id=comment_id,
                message=private_message_text,
                recipient_id_fallback=commenter_id,
            )
            if not recipient_id:
                raise RuntimeError("oauth private reply unavailable")
            with session_factory.begin() as db_session:
                record_oauth_direct_message_interaction(
                    db_session,
                    route.instagram_account_id,
                    comment_id,
                    recipient_id,
                )
                complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
            print("OAUTH FOLLOW-UP: waiting_for_recipient_reply")
            return

        if not media_id:
            raise RuntimeError("oauth prompt media unavailable")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=REQUEST_COOLDOWN_MINUTES)
        with session_factory() as db_session:
            cooling_down = oauth_request_on_cooldown(db_session, route.instagram_account_id, media_id, commenter_id, cutoff=cutoff)
        if cooling_down:
            _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
            return
        public_id = _oauth_prompt_post_for_comment(session_factory, route.instagram_account_id, entry_external_id, media_id, api_key=account_api_key)
        if not public_id:
            # v2: extraction failed — fall back to the ready DM/Link
            # message instead of going silent (golden rule).
            print("OAUTH ENGAGEMENT: prompt extraction failed; falling back to ready DM.")
            if _oauth_prompt_extraction_fallback(
                session_factory,
                route,
                entry_external_id,
                automation_config,
                comment_id,
                commenter_id,
            ):
                return
            raise RuntimeError("oauth private reply unavailable")
        page_url = f"{public_base_url.rstrip('/')}/p/o/{public_id}"
        if automation_config.public_reply_enabled:
            if not send_oauth_public_comment_reply(session_factory, instagram_account_id=route.instagram_account_id, instagram_user_id=entry_external_id, comment_id=comment_id, message=automation_config.public_reply_text):
                raise RuntimeError("oauth public reply unavailable")
        # Two-step DM flow:
        # 1. Send the first private DM with a visible "View prompts" quick
        #    reply (no URL, no web_url button, no instruction to type a
        #    legacy ACCESS/SEND keyword).
        # 2. Save pending access with the prompt page URL so that when the
        #    user presses the quick reply, the second DM with the real
        #    prompt-page link is unlocked.
        recipient_id = send_oauth_private_access_reply(
            session_factory,
            instagram_account_id=route.instagram_account_id,
            instagram_user_id=entry_external_id,
            comment_id=comment_id,
            message=automation_config.prompt_opening_message_text,
        )
        # The comment webhook also contains the commenter's Instagram-scoped ID,
        # so use it as a fallback if Meta did not return recipient_id.
        recipient_id = recipient_id or commenter_id
        if not recipient_id:
            raise RuntimeError("oauth private reply unavailable")
        with session_factory.begin() as db_session:
            save_oauth_pending_access(db_session, route.instagram_account_id, recipient_id, page_url)
            record_oauth_request(db_session, route.instagram_account_id, media_id, comment_id, recipient_id, username, page_url)
            followup_enabled = getattr(automation_config, "followup_enabled", True)
            if followup_enabled is None:
                followup_enabled = True
            followup_delay_minutes = getattr(automation_config, "followup_delay_minutes", 360)
            try:
                followup_delay_minutes = int(followup_delay_minutes)
            except (TypeError, ValueError):
                followup_delay_minutes = 360
            if followup_enabled:
                schedule_oauth_followup(
                    db_session,
                    instagram_account_id=route.instagram_account_id,
                    source_message_id=comment_id,
                    recipient_id=recipient_id,
                    page_url=page_url,
                    run_at=datetime.now(timezone.utc) + timedelta(minutes=followup_delay_minutes),
                )
            complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
    except Exception as exc:
        _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment", failed=True)
        _log_oauth_engagement_failure(exc, context="comment")


def _process_oauth_message(event, route, entry_external_id, session_factory):
    message = event.get("message") if isinstance(event, dict) else None
    sender = event.get("sender") if isinstance(event, dict) else None
    if not isinstance(message, dict) or not isinstance(sender, dict) or message.get("is_echo"):
        return
    message_id = str(message.get("mid") or "").strip()
    sender_id = str(sender.get("id") or "").strip()
    if not message_id or not sender_id:
        return
    with session_factory.begin() as db_session:
        if not claim_oauth_event(db_session, route.instagram_account_id, message_id, "message"):
            return
    # Only the explicit "View prompts" quick-reply payload (VIEW_AI_PROMPTS)
    # unlocks the prompt-link button for the new two-step flow.
    # Backward compatibility: legacy ACCESS/SEND keywords remain supported
    # for already-issued legacy pending flows.
    quick = message.get("quick_reply") if isinstance(message.get("quick_reply"), dict) else {}
    quick_payload = str(quick.get("payload") or "").strip().upper()
    words = _oauth_words(message.get("text"))
    is_view_prompts_quick_reply = quick_payload == "VIEW_AI_PROMPTS"
    is_legacy_access = "access" in words or "send" in words
    with session_factory() as db_session:
        automation_config = oauth_automation_runtime_config(
            db_session,
            instagram_account_id=route.instagram_account_id,
        )
    if (
        automation_config is not None
        and automation_config.delivery_mode in {"direct_message", "link"}
    ):
        recipient = event.get("recipient")
        if (
            sender_id == entry_external_id
            or not isinstance(recipient, dict)
            or str(recipient.get("id") or "").strip() != entry_external_id
            or event.get("is_non_reply") or event.get("read") or event.get("delivery")
            or message.get("is_deleted")
            or not (str(message.get("text") or "").strip() or quick_payload
                    or message.get("attachments") or message.get("has_attachments"))
        ):
            print("OAUTH FOLLOW-UP: recipient reply skipped. reason=unsupported_event_shape")
            _oauth_complete(session_factory, route.instagram_account_id, message_id, "message")
            return
        followup_message_text = str(automation_config.followup_message_text or "").strip()
        if (
            not automation_config.enabled
            or not automation_config.followup_enabled
            or not followup_message_text
        ):
            print("OAUTH FOLLOW-UP: recipient reply skipped. reason=followup_disabled")
            _oauth_complete(session_factory, route.instagram_account_id, message_id, "message")
            return
        current = datetime.now(timezone.utc)
        try:
            timestamp = event.get("timestamp")
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise ValueError
            replied_at = datetime.fromtimestamp(timestamp / 1000, timezone.utc)
            if replied_at > current:
                raise ValueError
        except (ValueError, TypeError, OverflowError, OSError):
            print("OAUTH FOLLOW-UP: recipient reply skipped. reason=invalid_reply_timestamp")
            _oauth_complete(session_factory, route.instagram_account_id, message_id, "message")
            return
        run_at = replied_at + timedelta(minutes=automation_config.followup_delay_minutes)
        window_expires_at = replied_at + DIRECT_MESSAGE_RESPONSE_WINDOW
        if run_at >= window_expires_at or current >= window_expires_at:
            print("OAUTH FOLLOW-UP: recipient reply skipped. reason=outside_provider_window")
            _oauth_complete(session_factory, route.instagram_account_id, message_id, "message")
            return
        with session_factory.begin() as db_session:
            interaction = consume_oauth_direct_message_interaction(
                db_session,
                route.instagram_account_id,
                sender_id,
                message_id,
                replied_at=replied_at,
            )
            if interaction is None:
                print("OAUTH FOLLOW-UP: recipient reply skipped. reason=no_prior_direct_interaction")
                complete_oauth_event(
                    db_session, route.instagram_account_id, message_id, "message"
                )
                return
            print("OAUTH FOLLOW-UP: recipient_reply_eligible")
            scheduled_followup = schedule_oauth_followup(
                db_session,
                instagram_account_id=route.instagram_account_id,
                source_message_id=message_id,
                recipient_id=sender_id,
                message_text=followup_message_text,
                provider_window_expires_at=window_expires_at,
                run_at=run_at,
                now=current,
            )
            if scheduled_followup is None:
                print("OAUTH FOLLOW-UP: recipient reply skipped. reason=cooldown")
            elif scheduled_followup.source_message_id == message_id:
                print("OAUTH FOLLOW-UP: job_scheduled_after_reply")
            else:
                print("OAUTH FOLLOW-UP: recipient reply skipped. reason=active_job")
            complete_oauth_event(
                db_session, route.instagram_account_id, message_id, "message"
            )
        return
    if not is_view_prompts_quick_reply and not is_legacy_access:
        # Arbitrary text reply: cancel any queued follow-up because the
        # person has already re-engaged, but do NOT unlock the prompt link.
        with session_factory.begin() as db_session:
            cancel_oauth_followups(
                db_session,
                instagram_account_id=route.instagram_account_id,
                recipient_id=sender_id,
            )
            complete_oauth_event(db_session, route.instagram_account_id, message_id, "message")
        return
    try:
        with session_factory() as db_session:
            page_url = get_oauth_pending_access(db_session, route.instagram_account_id, sender_id)
        if not page_url:
            _oauth_complete(session_factory, route.instagram_account_id, message_id, "message")
            return
        if not send_oauth_prompt_access_button(session_factory, instagram_account_id=route.instagram_account_id, instagram_user_id=entry_external_id, recipient_id=sender_id, page_url=page_url):
            raise RuntimeError("oauth prompt access unavailable")
        # Only consume pending access after successful button delivery.
        with session_factory.begin() as db_session:
            pop_oauth_pending_access(db_session, route.instagram_account_id, sender_id)
            followup_enabled = getattr(automation_config, "followup_enabled", True)
            if followup_enabled is None:
                followup_enabled = True
            followup_delay_minutes = getattr(automation_config, "followup_delay_minutes", 360)
            try:
                followup_delay_minutes = int(followup_delay_minutes)
            except (TypeError, ValueError):
                followup_delay_minutes = 360
            if followup_enabled:
                schedule_oauth_followup(
                    db_session,
                    instagram_account_id=route.instagram_account_id,
                    source_message_id=message_id,
                    recipient_id=sender_id,
                    page_url=page_url,
                    run_at=datetime.now(timezone.utc) + timedelta(minutes=followup_delay_minutes),
                )
            complete_oauth_event(db_session, route.instagram_account_id, message_id, "message")
    except Exception as exc:
        _oauth_complete(session_factory, route.instagram_account_id, message_id, "message", failed=True)
        _log_oauth_engagement_failure(exc, context="message")


def process_due_oauth_followups(now=None):
    """Process durable OAuth jobs without touching legacy JSON follow-ups."""
    session_factory = _oauth_session_factory()
    if session_factory is None:
        return
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        with session_factory.begin() as db_session:
            reap_expired_oauth_followup_leases(db_session, now=current)
            claimed_jobs = claim_due_oauth_followups(db_session, now=current)
    except Exception as exc:
        _log_oauth_followup_claim_failure(exc)
        return

    for job in claimed_jobs:
        try:
            delivery_time = current if now is not None else datetime.now(timezone.utc)
            if (
                job.provider_window_expires_at is not None
                and delivery_time >= job.provider_window_expires_at
            ):
                with session_factory.begin() as db_session:
                    finalize_oauth_followup_failure(
                        db_session,
                        job.id,
                        job.lease_token,
                        category="outside_provider_window",
                        retryable=False,
                        now=current,
                    )
                print("OAUTH FOLLOW-UP: delivery skipped. reason=outside_provider_window")
                continue
            with session_factory() as db_session:
                external_id = oauth_followup_account_external_id(
                    db_session, job.instagram_account_id
                )
            outcome = send_oauth_delayed_followup(
                session_factory,
                instagram_account_id=job.instagram_account_id,
                instagram_user_id=external_id,
                recipient_id=job.recipient_id,
                message_text=job.message_text,
                provider_window_expires_at=job.provider_window_expires_at,
            )
            with session_factory.begin() as db_session:
                if outcome == "success":
                    finalize_oauth_followup_success(db_session, job.id, job.lease_token, now=current)
                elif outcome == "retryable_pre_dispatch":
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category="credential_unavailable", retryable=True, now=current,
                    )
                elif outcome == "provider_rejected":
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category="provider_rejected", retryable=False, now=current,
                    )
                elif outcome in {"capability_unavailable", "outside_provider_window"}:
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category=outcome, retryable=False, now=current,
                    )
                else:
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category="ambiguous_graph_failure", retryable=False, now=current,
                    )
        except Exception:
            try:
                with session_factory.begin() as db_session:
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category="database_unavailable", retryable=True, now=current,
                    )
            except Exception:
                pass
            print("OAUTH FOLLOW-UP: processing unavailable.")


def process_oauth_webhook_entry(entry, route, entry_external_id, public_base_url):
    """Handle OAuth events only; no legacy handler, JSON store, or token fallback."""
    session_factory = _oauth_session_factory()
    if session_factory is None or not isinstance(entry, dict):
        return
    if oauth_engagement_capability_state(
        session_factory,
        instagram_account_id=route.instagram_account_id,
        instagram_user_id=entry_external_id,
    ) != "active":
        return
    changes = entry.get("changes", [])
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict) and change.get("field") == "comments" and isinstance(change.get("value"), dict):
                _process_oauth_comment(change["value"], route, entry_external_id, public_base_url, session_factory)
    messaging_events = entry.get("messaging", [])
    if isinstance(messaging_events, list):
        for event in messaging_events:
            if _messaging_event_matches_entry(event, entry_external_id):
                _process_oauth_message(event, route, entry_external_id, session_factory)


def _oauth_webhook_entry_from_work(work):
    if work.event_type == "comment":
        return {
            "id": work.instagram_user_id,
            "changes": [{"field": "comments", "value": work.payload}],
        }
    if work.event_type == "message":
        return {"id": work.instagram_user_id, "messaging": [work.payload]}
    return None


def process_due_oauth_webhook_work(now=None):
    """Consume durable ingress without replaying terminal or ambiguous work."""
    session_factory = _oauth_session_factory()
    if session_factory is None:
        return
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        with session_factory.begin() as db_session:
            claimed_work = claim_due_oauth_webhook_work(db_session, now=current)
    except Exception:
        print("OAUTH INGRESS: work claim unavailable.")
        return

    for work in claimed_work:
        try:
            with session_factory() as db_session:
                account_matches = oauth_webhook_work_account_matches(
                    db_session,
                    instagram_account_id=work.instagram_account_id,
                    instagram_user_id=work.instagram_user_id,
                )
            entry = _oauth_webhook_entry_from_work(work)
            if not account_matches or entry is None:
                raise RuntimeError("OAuth ingress account binding unavailable")
            route = InstagramAccountRoute(work.instagram_account_id, "oauth")
            process_oauth_webhook_entry(
                entry, route, work.instagram_user_id, work.public_base_url
            )
        except Exception:
            try:
                with session_factory.begin() as db_session:
                    fail_oauth_webhook_work(
                        db_session, work.id, work.lease_token, now=current
                    )
            except Exception:
                pass
            print("OAUTH INGRESS: work processing unavailable.")
            continue
        try:
            with session_factory.begin() as db_session:
                complete_oauth_webhook_work(
                    db_session, work.id, work.lease_token, now=current
                )
        except Exception:
            print("OAUTH INGRESS: work completion unavailable.")


def process_webhook_background(
    data,
    public_base_url,
    *,
    process_oauth=True,
    process_durable_oauth=True,
):
    """Dispatch each signed entry independently, with no cross-tenant fallback."""

    entries = data.get("entry", []) if isinstance(data, dict) else []
    if not isinstance(entries, list):
        return

    for entry in entries:
        entry_external_id = _webhook_entry_external_id(entry)
        if entry_external_id is None:
            print("WEBHOOK ROUTING: entry skipped (invalid envelope).")
            continue
        print(f"WEBHOOK ROUTING: resolving entry_external_id={entry_external_id}")
        try:
            route = _resolve_webhook_entry_route(entry_external_id)
        except RuntimeAccountRouteUnavailableError:
            print(f"WEBHOOK ROUTING: unavailable entry_external_id={entry_external_id} reason=account_route_unavailable")
            continue
        except Exception as exc:
            print(f"WEBHOOK ROUTING: unexpected failure entry_external_id={entry_external_id} exception={type(exc).__name__}")
            continue

        if route.connection_mode == "oauth":
            print("WEBHOOK ROUTING: oauth_route_selected=True")
            if not process_oauth:
                continue
            try:
                process_oauth_webhook_entry(entry, route, entry_external_id, public_base_url)
            except Exception:
                print("WEBHOOK ROUTING: OAuth entry skipped (engagement unavailable).")
            continue
        try:
            _process_legacy_webhook_entry(entry, entry_external_id, public_base_url)
        except Exception:
            print("WEBHOOK ROUTING: entry skipped (legacy processing failed).")
    if not process_oauth and process_durable_oauth:
        process_due_oauth_webhook_work()


def _configured_public_base_url() -> str | None:
    """Return a safe configured HTTPS base URL, if this deployment provides one."""
    try:
        parsed = urlsplit(PUBLIC_BASE_URL)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _public_webhook_base_url() -> str:
    """Keep generated links public when a production proxy terminates TLS."""
    configured = _configured_public_base_url()
    if configured is not None:
        return configured
    request_base = request.host_url.rstrip("/")
    if not IS_PRODUCTION:
        return request_base
    try:
        parsed = urlsplit(request_base)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "http"
            and parsed.netloc
            and hostname
            and hostname not in {"localhost", "127.0.0.1", "::1"}
            and not hostname.endswith((".local", ".internal"))
        ):
            return urlunsplit(("https", parsed.netloc, parsed.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        pass
    return request_base


# ==================================================
# RECEIVE WEBHOOK
# ==================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def receive_webhook():

    signature_valid = verify_meta_signature()
    if signature_valid is None:
        if not app.config.get("WEBHOOK_UNSIGNED_COMPATIBILITY", False):
            return "Webhook signature verification is required.", 503
    if signature_valid is False:
        return "Invalid webhook signature.", 403

    data = request.get_json(
        silent=True
    ) or {}

    public_base_url = _public_webhook_base_url()

    try:
        oauth_work_persisted = persist_oauth_webhook_ingress(data, public_base_url)
    except Exception:
        # Meta must redeliver if a routed OAuth event could not be made durable.
        print("OAUTH INGRESS: persistence unavailable.")
        return "Webhook ingress is temporarily unavailable.", 503

    worker = threading.Thread(

        target=
            process_webhook_background,

        args=(
            data,
            public_base_url
        ),

        kwargs={
            "process_oauth": False,
            "process_durable_oauth": oauth_work_persisted > 0,
        },

        daemon=True
    )

    worker.start()

    # Meta receives 200 immediately
    return "EVENT_RECEIVED", 200


# ==================================================
# RUN
# ==================================================
# ============================================================
# DASHBOARD - CONTENT GENERATOR (DISABLED IN V2 — see below)
# ============================================================

@app.route("/api/generate-content", methods=["POST"])
def generate_dashboard_content():
    # Postenza v2: AI content generation is disabled. Gemini is used only
    # to extract prompts written in images; use ready replies instead.
    return {
        "error": (
            "Content generation is disabled in Postenza v2. "
            "Gemini is used only to extract prompts written in images."
        )
    }, 410

# ============================================================
# DASHBOARD - DRAFT STORAGE / APPROVAL
# ============================================================

def load_dashboard_drafts():

    data = load_json_file(
        DRAFTS_FILE,
        []
    )

    if not isinstance(data, list):
        return []

    return data


@app.route("/api/approve-draft", methods=["POST"])
def approve_dashboard_draft():

    return {
        "error": "Post auto-publishing is disabled. Use the Post Assistant, then publish manually in Instagram."
    }, 410

    images = request.files.getlist("images")
    caption = str(request.form.get("caption", "")).strip()
    cta = str(request.form.get("cta", "")).strip()
    hashtags = str(request.form.get("hashtags", "")).strip()

    if not images:
        return {"error": "Please upload at least one image."}, 400

    if not caption:
        return {"error": "Caption is required."}, 400

    draft_id = uuid.uuid4().hex[:12]
    draft_folder = DRAFT_IMAGES_DIR / draft_id
    draft_folder.mkdir(parents=True, exist_ok=True)
    saved_images = []

    try:
        for index, image in enumerate(images[:10], start=1):
            if not image.filename:
                continue

            mime_type = (image.mimetype or "").lower()
            if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
                continue

            original_name = secure_filename(image.filename)
            suffix = Path(original_name).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = ".jpg"

            filename = f"{index:02d}{suffix}"
            file_path = draft_folder / filename
            image.save(str(file_path))

            saved_images.append(
                str(file_path.relative_to(DATA_DIR)).replace("\\", "/")
            )

        if not saved_images:
            return {"error": "No valid images were saved."}, 400

        now_iso = datetime.now(timezone.utc).isoformat()
        draft = {
            "id": draft_id,
            "status": "approved",
            "caption": caption,
            "cta": cta,
            "hashtags": hashtags,
            "images": saved_images,
            "created_at": now_iso,
            "approved_at": now_iso,
            "scheduled_for": None,
            "published_at": None,
            "instagram_media_id": None
        }

        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            drafts.insert(0, draft)
            if len(drafts) > 500:
                drafts = drafts[:500]
            save_json_file(DRAFTS_FILE, drafts)

        print("Dashboard draft approved:", draft_id)
        return {
            "success": True,
            "draft_id": draft_id,
            "status": "approved"
        }, 200

    except Exception as error:
        print("APPROVE DRAFT ERROR:", type(error).__name__)
        return {"error": "Could not save the draft."}, 500


@app.route("/api/drafts", methods=["GET"])
def get_dashboard_drafts():

    drafts = load_dashboard_drafts()
    result = []

    for draft in drafts[:20]:
        images = draft.get("images", [])
        if not isinstance(images, list):
            images = []

        image_urls = []
        draft_id = str(draft.get("id", "") or "")
        for image_path in images:
            filename = Path(str(image_path or "")).name
            if draft_id and filename:
                image_urls.append(
                    url_for(
                        "dashboard_draft_image",
                        draft_id=draft_id,
                        filename=filename
                    )
                )

        result.append({
            "id": draft_id,
            "status": draft.get("status", ""),
            "caption": draft.get("caption", ""),
            "cta": draft.get("cta", ""),
            "hashtags": draft.get("hashtags", ""),
            "image_count": len(images),
            "image_urls": image_urls,
            "created_at": draft.get("created_at", ""),
            "scheduled_for": draft.get("scheduled_for"),
            "scheduled_local": draft.get("scheduled_local"),
            "scheduled_timezone": draft.get("scheduled_timezone"),
            "schedule_error": draft.get("schedule_error"),
            "published_at": draft.get("published_at"),
            "instagram_media_id": draft.get("instagram_media_id"),
            "instagram_permalink": draft.get("instagram_permalink")
        })

    return {"drafts": result}, 200


@app.route("/api/instagram-published", methods=["GET"])
def get_instagram_published_posts():
    """Read the latest published posts directly from Instagram.

    Instagram is the source of truth for the Published view, so the dashboard
    still shows posts even if a local draft status was lost during a deploy or
    concurrent file write.
    """
    if not INSTAGRAM_ACCESS_TOKEN:
        return {"error": "Instagram access token is missing."}, 500

    try:
        response = requests.get(
            f"https://graph.instagram.com/{API_VERSION}/{INSTAGRAM_USER_ID}/media",
            params={
                "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
                "limit": 25,
                "access_token": INSTAGRAM_ACCESS_TOKEN
            },
            timeout=30
        )
        data = response.json()
        if not response.ok:
            error_message = (data.get("error") or {}).get("message") or "Could not load Instagram posts."
            return {"error": error_message}, response.status_code

        posts = data.get("data", [])
        if not isinstance(posts, list):
            posts = []

        cleaned = []
        for item in posts:
            if not isinstance(item, dict):
                continue
            cleaned.append({
                "id": str(item.get("id", "") or ""),
                "caption": str(item.get("caption", "") or ""),
                "media_type": str(item.get("media_type", "") or ""),
                "media_url": str(item.get("media_url", "") or ""),
                "thumbnail_url": str(item.get("thumbnail_url", "") or ""),
                "permalink": str(item.get("permalink", "") or ""),
                "timestamp": str(item.get("timestamp", "") or "")
            })

        return {"posts": cleaned}, 200

    except requests.RequestException as error:
        print("INSTAGRAM PUBLISHED POSTS ERROR:", type(error).__name__)
        return {"error": "Could not connect to Instagram."}, 502
    except Exception as error:
        print("INSTAGRAM PUBLISHED POSTS ERROR:", type(error).__name__)
        return {"error": "Could not load Instagram posts."}, 500




# ============================================================
# V0.2 - ADVANCED INSTAGRAM ANALYTICS
# ============================================================

def build_advanced_analytics(posts, followers_count=0):
    """Derive read-only advanced metrics from recent Instagram media records."""
    rows = [item for item in (posts or []) if isinstance(item, dict)]
    count = len(rows)

    def number(item, key):
        try:
            return max(0, int(item.get(key, 0) or 0))
        except (TypeError, ValueError):
            try:
                return max(0, int(float(item.get(key, 0) or 0)))
            except (TypeError, ValueError):
                return 0

    def media_label(value):
        value = str(value or "").upper()
        if value in {"REELS", "REEL", "VIDEO"}:
            return "Reel / Video"
        if value == "CAROUSEL_ALBUM":
            return "Carousel"
        if value == "IMAGE":
            return "Image"
        return value.title() if value else "Other"

    def engagement(item):
        return (
            number(item, "like_count")
            + number(item, "comments_count")
            + number(item, "saved")
            + number(item, "shares")
        )

    def performance_score(item):
        # A transparent comparative score, not an Instagram-defined metric.
        # It balances intent-heavy interactions with distribution metrics.
        return (
            engagement(item)
            + (number(item, "reach") * 0.10)
            + (number(item, "views") * 0.05)
        )

    totals = {
        "likes": sum(number(item, "like_count") for item in rows),
        "comments": sum(number(item, "comments_count") for item in rows),
        "reach": sum(number(item, "reach") for item in rows),
        "saved": sum(number(item, "saved") for item in rows),
        "shares": sum(number(item, "shares") for item in rows),
        "views": sum(number(item, "views") for item in rows),
    }
    total_engagement = totals["likes"] + totals["comments"] + totals["saved"] + totals["shares"]

    if totals["reach"] > 0:
        engagement_rate = round((total_engagement / totals["reach"]) * 100, 2)
        engagement_basis = "reach"
    elif count > 0 and int(followers_count or 0) > 0:
        average_engagement = total_engagement / count
        engagement_rate = round((average_engagement / int(followers_count)) * 100, 2)
        engagement_basis = "followers"
    else:
        engagement_rate = 0.0
        engagement_basis = "reach"

    average = {
        key: round(value / count, 1) if count else 0
        for key, value in totals.items()
    }
    for key in ("likes", "comments", "reach", "saved", "shares", "views"):
        value = average[key]
        if float(value).is_integer():
            average[key] = int(value)

    ranked = sorted(rows, key=performance_score, reverse=True)
    top_posts = []
    for item in ranked[:5]:
        enriched = dict(item)
        enriched["performance_score"] = round(performance_score(item), 2)
        enriched["engagement_actions"] = engagement(item)
        top_posts.append(enriched)

    by_type = {}
    for item in rows:
        label = media_label(item.get("media_type"))
        group = by_type.setdefault(label, {"count": 0, "score": 0.0, "engagement": 0})
        group["count"] += 1
        group["score"] += performance_score(item)
        group["engagement"] += engagement(item)

    type_rows = []
    for label, values in by_type.items():
        type_rows.append({
            "type": label,
            "posts": values["count"],
            "average_score": round(values["score"] / values["count"], 2),
            "average_engagement": round(values["engagement"] / values["count"], 1),
        })
    type_rows.sort(key=lambda item: (item["average_score"], item["average_engagement"]), reverse=True)
    best_content_type = type_rows[0] if type_rows else {
        "type": "Not enough data", "posts": 0, "average_score": 0, "average_engagement": 0
    }

    timezone_local = ZoneInfo("Africa/Casablanca")
    hour_groups = {}
    for item in rows:
        raw_timestamp = str(item.get("timestamp", "") or "").strip()
        if not raw_timestamp:
            continue
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local = parsed.astimezone(timezone_local)
        except Exception:
            continue
        hour = int(local.hour)
        group = hour_groups.setdefault(hour, {"count": 0, "score": 0.0})
        group["count"] += 1
        group["score"] += performance_score(item)

    best_hours = [
        {
            "hour": f"{hour:02d}:00",
            "posts": values["count"],
            "average_score": round(values["score"] / values["count"], 2),
        }
        for hour, values in hour_groups.items()
    ]
    best_hours.sort(key=lambda item: item["average_score"], reverse=True)
    best_hours = best_hours[:3]

    chronological = []
    for item in rows:
        raw_timestamp = str(item.get("timestamp", "") or "").strip()
        try:
            parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            parsed = datetime.min.replace(tzinfo=timezone.utc)
        chronological.append((parsed, item))
    chronological.sort(key=lambda pair: pair[0])

    if len(chronological) >= 4:
        split = len(chronological) // 2
        older = [item for _, item in chronological[:split]]
        newer = [item for _, item in chronological[split:]]
        older_avg = sum(performance_score(item) for item in older) / len(older)
        newer_avg = sum(performance_score(item) for item in newer) / len(newer)
        if older_avg > 0:
            change = ((newer_avg - older_avg) / older_avg) * 100
        else:
            change = 100.0 if newer_avg > 0 else 0.0
        direction = "up" if change > 5 else "down" if change < -5 else "flat"
        growth_trend = {
            "direction": direction,
            "change_percent": round(change, 1),
            "recent_average_score": round(newer_avg, 2),
            "previous_average_score": round(older_avg, 2),
        }
    else:
        growth_trend = {
            "direction": "flat",
            "change_percent": 0.0,
            "recent_average_score": 0,
            "previous_average_score": 0,
        }

    return {
        "posts_analyzed": count,
        "engagement_rate": {"value": engagement_rate, "basis": engagement_basis},
        "average_per_post": average,
        "best_content_type": best_content_type,
        "content_types": type_rows,
        "best_posting_hours": best_hours,
        "growth_trend": growth_trend,
        "top_posts": top_posts,
        "score_note": (
            "Comparative performance score = engagement actions + 10% of reach + 5% of views. "
            "It is used only to compare this account's recent posts."
        ),
    }


def generate_advanced_analytics_summary(advanced):
    """Return a short AI interpretation with a deterministic fallback."""
    best_type = (advanced.get("best_content_type") or {}).get("type", "your strongest format")
    hours = advanced.get("best_posting_hours") or []
    best_hour = hours[0].get("hour") if hours else "your usual high-performing window"
    trend = advanced.get("growth_trend") or {}
    direction = trend.get("direction", "flat")
    change = trend.get("change_percent", 0)

    fallback = (
        f"{best_type} is currently the strongest recent format. "
        f"Your best observed posting window starts around {best_hour}. "
        f"Recent performance trend is {direction} ({change:+.1f}%)."
    )

    # Postenza v2: no Gemini summary — the deterministic fallback above
    # is the whole answer (Gemini is vision-only, prompt extraction).
    return fallback


# In-memory cache for the /api/instagram-analytics success payload only
# (the endpoint serves the single legacy account). Error responses are NOT
# cached, so a transient Graph failure does not linger. TTL: 5 minutes.
_INSTAGRAM_ANALYTICS_CACHE = {"fetched_at": 0.0, "payload": None}
_INSTAGRAM_ANALYTICS_CACHE_TTL_SECONDS = 300


@app.route("/api/instagram-analytics", methods=["GET"])
def get_instagram_analytics():
    """Return resilient Instagram analytics without blocking Railway.

    V0.2 fix: the previous implementation made up to five sequential insight
    requests for every post. With 30 posts that could mean ~150 Graph API calls,
    which can exceed the web request timeout; Railway then returns an HTML 5xx
    page and the dashboard reports ``Unexpected token '<'`` while parsing JSON.

    This version keeps the same JSON contract but fetches each post concurrently
    and requests supported insight metrics in one Graph API call per post. If a
    media type rejects an insight bundle, public counters still load and the
    unsupported insights remain zero instead of breaking the whole page.
    """
    if not INSTAGRAM_ACCESS_TOKEN:
        return {"error": "Instagram access token is missing."}, 500

    if (
        _INSTAGRAM_ANALYTICS_CACHE["payload"] is not None
        and time.time() - _INSTAGRAM_ANALYTICS_CACHE["fetched_at"] < _INSTAGRAM_ANALYTICS_CACHE_TTL_SECONDS
    ):
        return _INSTAGRAM_ANALYTICS_CACHE["payload"]

    def as_int(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            try:
                return int(float(value or 0))
            except (TypeError, ValueError):
                return 0

    def graph_get(path, params=None, timeout=12):
        query = dict(params or {})
        query["access_token"] = INSTAGRAM_ACCESS_TOKEN
        response = requests.get(
            f"https://graph.instagram.com/{API_VERSION}/{path.lstrip('/')}",
            params=query,
            timeout=timeout,
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        return response, data

    def insight_value(row):
        if not isinstance(row, dict):
            return 0
        if "value" in row:
            return as_int(row.get("value"))
        values = row.get("values", [])
        if isinstance(values, list) and values:
            last = values[-1] if isinstance(values[-1], dict) else {}
            return as_int(last.get("value"))
        return 0

    def build_post(raw):
        media_id = str(raw.get("id", "") or "")
        post = {
            "id": media_id,
            "caption": str(raw.get("caption", "") or ""),
            "media_type": str(raw.get("media_type", "") or ""),
            "media_url": str(raw.get("media_url", "") or ""),
            "thumbnail_url": str(raw.get("thumbnail_url", "") or ""),
            "permalink": str(raw.get("permalink", "") or ""),
            "timestamp": str(raw.get("timestamp", "") or ""),
            "like_count": as_int(raw.get("like_count")),
            "comments_count": as_int(raw.get("comments_count")),
            "reach": 0,
            "saved": 0,
            "shares": 0,
            "views": 0,
            "impressions": 0,
            "available_insights": [],
        }
        if not media_id:
            return post

        # If list-level counters were unavailable, recover them without making
        # this failure fatal. This normally does not add a request.
        if "like_count" not in raw or "comments_count" not in raw:
            try:
                count_response, count_data = graph_get(
                    media_id, {"fields": "like_count,comments_count"}
                )
                if count_response.ok and isinstance(count_data, dict):
                    post["like_count"] = as_int(count_data.get("like_count"))
                    post["comments_count"] = as_int(count_data.get("comments_count"))
            except requests.RequestException:
                pass

        # One insights request per media instead of five sequential requests.
        # `impressions` is intentionally excluded from the bundle because newer
        # Instagram API versions/media types can reject it. The dashboard already
        # treats missing metrics as zero.
        try:
            insight_response, insight_data = graph_get(
                f"{media_id}/insights",
                {"metric": "reach,saved,shares,views"},
            )
            if insight_response.ok and isinstance(insight_data, dict):
                rows = insight_data.get("data", [])
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        metric = str(row.get("name", "") or "")
                        if metric in {"reach", "saved", "shares", "views"}:
                            post[metric] = insight_value(row)
                            post["available_insights"].append(metric)
        except requests.RequestException:
            pass

        return post

    try:
        account = {
            "id": str(INSTAGRAM_USER_ID),
            "username": "",
            "followers_count": 0,
            "media_count": 0,
        }
        account_response, account_data = graph_get(
            str(INSTAGRAM_USER_ID),
            {"fields": "id,username,followers_count,media_count"},
        )
        if account_response.ok and isinstance(account_data, dict):
            account["id"] = str(account_data.get("id", INSTAGRAM_USER_ID) or INSTAGRAM_USER_ID)
            account["username"] = str(account_data.get("username", "") or "")
            account["followers_count"] = as_int(account_data.get("followers_count"))
            account["media_count"] = as_int(account_data.get("media_count"))

        media_response, media_data = graph_get(
            f"{INSTAGRAM_USER_ID}/media",
            {
                "fields": (
                    "id,caption,media_type,media_url,thumbnail_url,permalink,"
                    "timestamp,like_count,comments_count"
                ),
                "limit": 30,
            },
        )

        # Some account/API combinations reject one of the public counters.
        if not media_response.ok:
            media_response, media_data = graph_get(
                f"{INSTAGRAM_USER_ID}/media",
                {
                    "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
                    "limit": 30,
                },
            )

        if not media_response.ok:
            error_message = (
                (media_data.get("error") or {}).get("message")
                if isinstance(media_data, dict)
                else ""
            ) or "Could not load Instagram analytics."
            return {"error": error_message}, media_response.status_code

        raw_posts = media_data.get("data", []) if isinstance(media_data, dict) else []
        if not isinstance(raw_posts, list):
            raw_posts = []
        raw_posts = [item for item in raw_posts[:30] if isinstance(item, dict) and item.get("id")]

        # Keep the request comfortably below Railway's web timeout. Results are
        # restored to Instagram's original order after concurrent fetching.
        indexed_posts = {}
        if raw_posts:
            worker_count = min(8, len(raw_posts))
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                future_map = {
                    executor.submit(build_post, raw): index
                    for index, raw in enumerate(raw_posts)
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        indexed_posts[index] = future.result()
                    except Exception as error:
                        print("INSTAGRAM ANALYTICS MEDIA ERROR:", type(error).__name__)
                        indexed_posts[index] = build_post(raw_posts[index]) if False else {
                            "id": str(raw_posts[index].get("id", "") or ""),
                            "caption": str(raw_posts[index].get("caption", "") or ""),
                            "media_type": str(raw_posts[index].get("media_type", "") or ""),
                            "media_url": str(raw_posts[index].get("media_url", "") or ""),
                            "thumbnail_url": str(raw_posts[index].get("thumbnail_url", "") or ""),
                            "permalink": str(raw_posts[index].get("permalink", "") or ""),
                            "timestamp": str(raw_posts[index].get("timestamp", "") or ""),
                            "like_count": as_int(raw_posts[index].get("like_count")),
                            "comments_count": as_int(raw_posts[index].get("comments_count")),
                            "reach": 0, "saved": 0, "shares": 0, "views": 0,
                            "impressions": 0, "available_insights": [],
                        }

        posts = [indexed_posts[index] for index in sorted(indexed_posts)]

        totals = {
            "posts": len(posts),
            "likes": sum(as_int(item.get("like_count")) for item in posts),
            "comments": sum(as_int(item.get("comments_count")) for item in posts),
            "reach": sum(as_int(item.get("reach")) for item in posts),
            "saved": sum(as_int(item.get("saved")) for item in posts),
            "shares": sum(as_int(item.get("shares")) for item in posts),
            "views": sum(as_int(item.get("views")) for item in posts),
            "impressions": sum(as_int(item.get("impressions")) for item in posts),
        }

        ranked = sorted(
            posts,
            key=lambda item: (
                as_int(item.get("like_count"))
                + as_int(item.get("comments_count"))
                + as_int(item.get("saved"))
                + as_int(item.get("shares"))
            ),
            reverse=True,
        )

        advanced = build_advanced_analytics(posts, account.get("followers_count", 0))
        advanced["ai_summary"] = generate_advanced_analytics_summary(advanced)

        payload = (
            {
                "account": account,
                "totals": totals,
                "posts": posts,
                "top_posts": ranked[:6],
                "advanced": advanced,
                "note": (
                    "Reach, saves, shares and views depend on the Instagram media type "
                    "and permissions available to this account. Analytics now uses a "
                    "bounded concurrent fetch to avoid web-request timeouts."
                ),
            },
            200,
        )
        _INSTAGRAM_ANALYTICS_CACHE["payload"] = payload
        _INSTAGRAM_ANALYTICS_CACHE["fetched_at"] = time.time()
        return payload

    except requests.RequestException as error:
        print("INSTAGRAM ANALYTICS CONNECTION ERROR:", type(error).__name__)
        return {"error": "Could not connect to Instagram analytics."}, 502
    except Exception as error:
        print("INSTAGRAM ANALYTICS ERROR:", type(error).__name__)
        return {"error": "Could not load Instagram analytics."}, 500




# ============================================================
# V0.2 - SMART CONTENT PLANNER
# ============================================================

@app.route("/api/content-planner", methods=["POST"])
def generate_content_planner():
    # Postenza v2: AI content planning is disabled. Gemini is used only
    # to extract prompts written in images; use ready replies instead.
    return {
        "error": (
            "Content planning is disabled in Postenza v2. "
            "Gemini is used only to extract prompts written in images."
        )
    }, 410


@app.route("/api/published-permalink/<draft_id>", methods=["GET"])
def get_published_permalink(draft_id):
    """Return and cache the official Instagram permalink for a published draft."""
    safe_draft_id = str(draft_id or "").strip()
    if not safe_draft_id:
        return {"error": "Draft ID is required."}, 400

    with STATE_LOCK:
        drafts = load_dashboard_drafts()
        draft = next(
            (item for item in drafts if str(item.get("id", "")) == safe_draft_id),
            None
        )
        if not draft:
            return {"error": "Draft not found."}, 404
        if draft.get("status") != "published":
            return {"error": "This draft is not published."}, 409
        cached_permalink = str(draft.get("instagram_permalink", "") or "").strip()
        media_id = str(draft.get("instagram_media_id", "") or "").strip()

    if cached_permalink:
        return {"success": True, "permalink": cached_permalink}, 200
    if not media_id:
        return {"error": "Instagram Media ID is missing for this published post."}, 404

    try:
        response = requests.get(
            f"https://graph.facebook.com/{API_VERSION}/{media_id}",
            params={
                "fields": "permalink",
                "access_token": INSTAGRAM_ACCESS_TOKEN
            },
            timeout=30
        )
        data = response.json()
        if not response.ok:
            error_message = (data.get("error") or {}).get("message") or "Could not get Instagram permalink."
            return {"error": error_message}, 502

        permalink = str(data.get("permalink", "") or "").strip()
        if not permalink:
            return {"error": "Instagram did not return a permalink."}, 502

        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            for item in drafts:
                if str(item.get("id", "")) == safe_draft_id:
                    item["instagram_permalink"] = permalink
                    break
            save_json_file(DRAFTS_FILE, drafts)

        return {"success": True, "permalink": permalink}, 200

    except Exception as error:
        print("INSTAGRAM PERMALINK ERROR:", type(error).__name__)
        return {"error": "Could not get Instagram permalink."}, 500


# ============================================================
# DASHBOARD - DRAFT IMAGE SERVING
# ============================================================

def detect_image_mimetype(image_bytes):

    if not image_bytes:
        return ""

    # JPEG
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # PNG
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # WEBP
    if (
        len(image_bytes) >= 12
        and image_bytes[:4] == b"RIFF"
        and image_bytes[8:12] == b"WEBP"
    ):
        return "image/webp"

    return ""


@app.route(
    "/draft-image/<draft_id>/<filename>",
    methods=["GET", "HEAD"]
)
def dashboard_draft_image(draft_id, filename):

    safe_draft_id = secure_filename(
        str(draft_id or "")
    )

    safe_filename = secure_filename(
        str(filename or "")
    )

    if not safe_draft_id or not safe_filename:
        return Response(status=404)

    file_path = (
        DRAFT_IMAGES_DIR
        / safe_draft_id
        / safe_filename
    )

    try:

        resolved_path = file_path.resolve()
        resolved_root = DRAFT_IMAGES_DIR.resolve()

        if resolved_root not in resolved_path.parents:
            return Response(status=404)

        if (
            not resolved_path.exists()
            or not resolved_path.is_file()
        ):
            return Response(status=404)

        image_bytes = resolved_path.read_bytes()

        if not image_bytes:
            return Response(status=404)

        mime_type = detect_image_mimetype(
            image_bytes
        )

        if not mime_type:
            print(
                "DRAFT IMAGE INVALID FORMAT:",
                resolved_path
            )

            return Response(
                "Invalid image format",
                status=415,
                mimetype="text/plain"
            )

        # Meta fetches image_url directly. Return a plain public image
        # response without Flask's download/content-disposition behavior.
        headers = {
            "Cache-Control": "public, max-age=86400",
            "Content-Length": str(len(image_bytes)),
            "Access-Control-Allow-Origin": "*",
            "X-Content-Type-Options": "nosniff"
        }

        if request.method == "HEAD":
            return Response(
                status=200,
                headers=headers,
                mimetype=mime_type
            )

        return Response(
            image_bytes,
            status=200,
            headers=headers,
            mimetype=mime_type
        )

    except Exception as error:

        print(
            "DRAFT IMAGE ERROR:",
            type(error).__name__
        )

        return Response(status=404)


# ============================================================
# DASHBOARD - CLOUDINARY MEDIA HOSTING
# ============================================================

def cloudinary_is_configured():

    return bool(
        CLOUDINARY_CLOUD_NAME
        and CLOUDINARY_API_KEY
        and CLOUDINARY_API_SECRET
    )


def upload_draft_image_to_cloudinary(draft_id, image_number, file_path):

    if not cloudinary_is_configured():
        return None, (
            "Cloudinary is not configured. "
            "Add CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY "
            "and CLOUDINARY_API_SECRET in Railway Variables."
        )

    try:
        resolved_path = Path(file_path).resolve()

        if not resolved_path.exists() or not resolved_path.is_file():
            return None, "Draft image file was not found."

        image_bytes = resolved_path.read_bytes()

        if not image_bytes:
            return None, "Draft image file is empty."

        if not image_bytes.startswith(b"\xff\xd8\xff"):
            return None, (
                "The approved image is not a real JPEG file. "
                "Upload it again and approve a new draft."
            )

        public_id = f"{draft_id}-{int(image_number):02d}"

        upload_url = (
            "https://api.cloudinary.com/v1_1/"
            f"{CLOUDINARY_CLOUD_NAME}/image/upload"
        )

        # Cloudinary supports HTTP Basic Authentication for server-side
        # Upload API calls. This avoids manual signature generation entirely
        # and prevents Invalid Signature errors caused by serialization
        # differences between signed parameters.
        data = {
            "folder": "imran-prompts",
            "format": "jpg",
            "overwrite": "true",
            "public_id": public_id
        }

        response = requests.post(
            upload_url,
            auth=(
                CLOUDINARY_API_KEY,
                CLOUDINARY_API_SECRET
            ),
            data=data,
            files={
                "file": (
                    f"{int(image_number):02d}.jpg",
                    image_bytes,
                    "image/jpeg"
                )
            },
            timeout=60
        )

        print("Cloudinary upload:", response.status_code)

        if not response.ok:
            print(response.text)
            return None, (
                "Cloudinary could not host the image: "
                + response.text[:500]
            )

        result = response.json()
        secure_url = str(result.get("secure_url", "") or "").strip()

        if not secure_url.startswith("https://"):
            return None, "Cloudinary did not return a public HTTPS image URL."

        print("Cloudinary hosted image:", secure_url)
        return secure_url, ""

    except requests.RequestException as error:
        print("CLOUDINARY REQUEST ERROR:", type(error).__name__)
        return None, "Cloudinary upload request failed: " + str(error)

    except Exception as error:
        print("CLOUDINARY UPLOAD ERROR:", type(error).__name__)
        return None, "Could not prepare the image for Cloudinary: " + str(error)




# ============================================================
# DAILY AUTO STORY AUTOMATION
# ============================================================

def default_auto_story_settings():
    return {
        "daily_enabled": AUTO_STORY_DAILY_ENABLED,
        "daily_time": AUTO_STORY_DAILY_TIME,
        "repeat_after_days": AUTO_STORY_REPEAT_AFTER_DAYS,
        "min_gap_hours": AUTO_STORY_MIN_GAP_HOURS,
    }


def load_auto_story_settings():
    data = load_json_file(AUTO_STORY_SETTINGS_FILE, {})
    settings = default_auto_story_settings()
    if isinstance(data, dict):
        for key in settings:
            if key in data:
                settings[key] = data[key]

    settings["daily_enabled"] = bool(settings.get("daily_enabled"))

    daily_time = str(settings.get("daily_time", AUTO_STORY_DAILY_TIME) or AUTO_STORY_DAILY_TIME).strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        daily_time = AUTO_STORY_DAILY_TIME
    settings["daily_time"] = daily_time

    try:
        settings["repeat_after_days"] = max(1, min(90, int(settings.get("repeat_after_days", AUTO_STORY_REPEAT_AFTER_DAYS))))
    except Exception:
        settings["repeat_after_days"] = AUTO_STORY_REPEAT_AFTER_DAYS

    try:
        settings["min_gap_hours"] = max(0, min(24, int(settings.get("min_gap_hours", AUTO_STORY_MIN_GAP_HOURS))))
    except Exception:
        settings["min_gap_hours"] = AUTO_STORY_MIN_GAP_HOURS

    return settings


@app.route("/api/auto-story-settings", methods=["GET", "POST"])
def auto_story_settings_api():
    if request.method == "GET":
        return {"settings": load_auto_story_settings()}, 200

    payload = request.get_json(silent=True) or {}
    current = load_auto_story_settings()

    if "daily_enabled" in payload:
        current["daily_enabled"] = bool(payload.get("daily_enabled"))
    if "daily_time" in payload:
        current["daily_time"] = str(payload.get("daily_time") or "").strip()
    if "repeat_after_days" in payload:
        current["repeat_after_days"] = payload.get("repeat_after_days")
    if "min_gap_hours" in payload:
        current["min_gap_hours"] = payload.get("min_gap_hours")

    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(current.get("daily_time", ""))):
        return {"error": "Daily time must use HH:MM."}, 400

    try:
        current["repeat_after_days"] = max(1, min(90, int(current.get("repeat_after_days", AUTO_STORY_REPEAT_AFTER_DAYS))))
        current["min_gap_hours"] = max(0, min(24, int(current.get("min_gap_hours", AUTO_STORY_MIN_GAP_HOURS))))
    except Exception:
        return {"error": "Invalid Auto Story numeric settings."}, 400

    current["daily_enabled"] = bool(current.get("daily_enabled"))
    save_json_file(AUTO_STORY_SETTINGS_FILE, current)
    return {"success": True, "settings": load_auto_story_settings()}, 200


def auto_story_fallback_comment(caption=""):
    caption = re.sub(r"\s+", " ", str(caption or "")).strip().lower()
    if "prompt" in caption or "comment ai" in caption:
        return "Missed this one? 👀 Comment AI for the prompt"
    return "Still one of my favorites ✨"


# Postenza v2: Gemini is vision-only (prompt extraction from images).
# Daily story overlays use ready templates only — no AI call, ever.
# The pool rotates by day of year so the overlay changes daily.
_DAILY_STORY_COMMENTS = (
    "Still one of my favorites ✨",
    "A little throwback for today ✨",
    "Worth another look 👀",
    "From the archive — enjoy ✨",
    "One more time, because it deserves it ✨",
)
_DAILY_STORY_PROMPT_COMMENTS = (
    "Missed this one? 👀 Comment AI for the prompt",
    "Want the prompt? Comment AI ✨",
    "Comment AI and I'll share the prompt 👀",
    "The prompt is one comment away — just say AI ✨",
    "Love this one? Comment AI for the prompt ✨",
)


def generate_daily_story_comment(caption=""):
    caption_lc = re.sub(r"\s+", " ", str(caption or "")).strip().lower()
    if "prompt" in caption_lc or "comment ai" in caption_lc:
        pool = _DAILY_STORY_PROMPT_COMMENTS
    else:
        pool = _DAILY_STORY_COMMENTS
    index = datetime.now(timezone.utc).timetuple().tm_yday % len(pool)
    return pool[index]


def _story_font(size, bold=True):
    """
    Load a real scalable TrueType font for Story overlays.

    Railway can fail to resolve "DejaVuSans-Bold.ttf" by basename only.
    Pillow then falls back to its tiny bitmap default font, which makes the
    Story caption almost unreadable. Prefer absolute Linux font paths first,
    then try normal font names as a local-development fallback.
    """
    if not PIL_AVAILABLE:
        return None

    if bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "DejaVuSans-Bold.ttf",
        ]

    for font_path in candidates:
        try:
            return ImageFont.truetype(font_path, int(size))
        except Exception:
            continue

    # Pillow 10.1+ ships a scalable built-in font. Unlike load_default()
    # without a size, this renders sharply at the requested pixel size and
    # does not depend on Linux/Windows system fonts being installed.
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:
        # Compatibility fallback for older Pillow versions.
        try:
            return ImageFont.load_default()
        except Exception:
            return None
    except Exception:
        return None


def _wrap_story_text(text, max_chars=28):
    """Wrap Story copy independently from the installed font metrics."""
    words = str(text or "").strip().split()
    lines = []
    current = ""

    for word in words:
        test = word if not current else current + " " + word
        if current and len(test) > max_chars:
            lines.append(current)
            current = word
        else:
            current = test

    if current:
        lines.append(current)

    # Story overlays should stay compact and readable.
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return lines[:2]


def _render_story_text_line(line, font, target_height=64, max_width=820):
    """
    Render one line to a transparent layer and force a readable visual size.

    This is intentionally independent of the host font installation. Even if
    Pillow falls back to its tiny bitmap font, the rendered text bitmap is
    enlarged to the target height before it is composited onto the Story.
    """
    line = str(line or "").strip()
    if not line:
        return None

    probe = Image.new("RGBA", (1600, 300), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)

    try:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=1)
        x0, y0, x1, y1 = bbox
    except Exception:
        x0, y0, x1, y1 = 0, 0, max(1, len(line) * 12), 24

    width = max(1, x1 - x0)
    height = max(1, y1 - y0)

    raw = Image.new("RGBA", (width + 16, height + 16), (0, 0, 0, 0))
    raw_draw = ImageDraw.Draw(raw)
    raw_draw.text(
        (8 - x0, 8 - y0),
        line,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=1,
        stroke_fill=(255, 255, 255, 255),
    )

    content_bbox = raw.getbbox()
    if content_bbox:
        raw = raw.crop(content_bbox)

    # First guarantee the requested text height.
    scale = target_height / max(1, raw.height)
    new_w = max(1, int(raw.width * scale))
    new_h = max(1, int(raw.height * scale))

    # Then keep the line within the Story safe width.
    if new_w > max_width:
        width_scale = max_width / new_w
        new_w = max(1, int(new_w * width_scale))
        new_h = max(1, int(new_h * width_scale))

    return raw.resize((new_w, new_h), Image.Resampling.LANCZOS)


def render_daily_story_image(image_bytes, comment):
    """Create a 1080x1920 Story while keeping the full original image visible."""
    if not PIL_AVAILABLE:
        return None, "Pillow is required for Daily Auto Story."

    try:
        source = Image.open(BytesIO(image_bytes)).convert("RGB")
        canvas_w, canvas_h = 1080, 1920

        bg = source.copy()
        scale = max(canvas_w / bg.width, canvas_h / bg.height)
        bg = bg.resize(
            (max(1, int(bg.width * scale)), max(1, int(bg.height * scale))),
            Image.Resampling.LANCZOS,
        )
        left = max(0, (bg.width - canvas_w) // 2)
        top = max(0, (bg.height - canvas_h) // 2)
        bg = bg.crop((left, top, left + canvas_w, top + canvas_h)).filter(
            ImageFilter.GaussianBlur(30)
        )
        overlay = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 54))
        canvas = Image.alpha_composite(bg.convert("RGBA"), overlay)

        fg = source.copy()
        fg.thumbnail((980, 1450), Image.Resampling.LANCZOS)
        x = (canvas_w - fg.width) // 2
        y = max(170, (canvas_h - fg.height) // 2 - 10)

        shadow = Image.new(
            "RGBA",
            (fg.width + 40, fg.height + 40),
            (0, 0, 0, 0),
        )
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            (20, 20, fg.width + 20, fg.height + 20),
            radius=28,
            fill=(0, 0, 0, 95),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(16))
        canvas.alpha_composite(shadow, (x - 20, y - 20))
        canvas.alpha_composite(fg.convert("RGBA"), (x, y))

        text = re.sub(r"\s+", " ", str(comment or "")).strip()
        if text:
            font = _story_font(68, True)
            lines = _wrap_story_text(text, max_chars=28)

            text_layers = []
            for line in lines:
                line = str(line or "").strip()
                if not line:
                    continue

                # Render the TrueType font directly at its final visual size.
                # Do NOT enlarge the rendered bitmap: that was the source of
                # the blurry/pixelated caption in the previous test.
                probe = Image.new("RGBA", (1000, 180), (0, 0, 0, 0))
                probe_draw = ImageDraw.Draw(probe)
                bbox = probe_draw.textbbox(
                    (0, 0),
                    line,
                    font=font,
                    stroke_width=0,
                )
                x0, y0, x1, y1 = bbox

                layer = Image.new(
                    "RGBA",
                    (max(1, x1 - x0) + 16, max(1, y1 - y0) + 16),
                    (0, 0, 0, 0),
                )
                layer_draw = ImageDraw.Draw(layer)
                layer_draw.text(
                    (8 - x0, 8 - y0),
                    line,
                    font=font,
                    fill=(255, 255, 255, 255),
                )

                crop = layer.getbbox()
                if crop:
                    layer = layer.crop(crop)

                # Reduce only if a line is unusually wide. Never upscale.
                if layer.width > 820:
                    scale = 820 / layer.width
                    layer = layer.resize(
                        (820, max(1, int(layer.height * scale))),
                        Image.Resampling.LANCZOS,
                    )

                text_layers.append(layer)

            if text_layers:
                line_gap = 24
                max_text_w = max(layer.width for layer in text_layers)
                total_text_h = (
                    sum(layer.height for layer in text_layers)
                    + line_gap * max(0, len(text_layers) - 1)
                )

                pad_x = 58
                pad_y = 42
                box_w = min(940, max(560, max_text_w + pad_x * 2))
                box_h = total_text_h + pad_y * 2
                box_x = (canvas_w - box_w) // 2
                box_y = canvas_h - box_h - 105

                box_layer = Image.new(
                    "RGBA",
                    (box_w, box_h),
                    (0, 0, 0, 0),
                )
                box_draw = ImageDraw.Draw(box_layer)
                box_draw.rounded_rectangle(
                    (0, 0, box_w - 1, box_h - 1),
                    radius=38,
                    fill=(0, 0, 0, 205),
                )

                ty = pad_y
                for layer in text_layers:
                    tx = (box_w - layer.width) // 2
                    box_layer.alpha_composite(layer, (tx, ty))
                    ty += layer.height + line_gap

                canvas.alpha_composite(box_layer, (box_x, box_y))

        output = BytesIO()
        canvas.convert("RGB").save(
            output,
            format="JPEG",
            quality=92,
            optimize=True,
        )
        return output.getvalue(), ""

    except Exception as error:
        return None, "Could not compose Daily Auto Story image: " + str(error)



def upload_daily_story_bytes_to_cloudinary(story_id, media_bytes):
    if not cloudinary_is_configured():
        return None, "Cloudinary is not configured."
    if not media_bytes:
        return None, "Daily Auto Story image is empty."

    upload_url = (
        "https://api.cloudinary.com/v1_1/"
        f"{CLOUDINARY_CLOUD_NAME}/image/upload"
    )
    data = {
        "folder": "imran-prompts/stories",
        "format": "jpg",
        "overwrite": "true",
        "public_id": f"daily-story-{story_id}",
    }

    try:
        response = requests.post(
            upload_url,
            auth=(CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET),
            data=data,
            files={"file": ("daily-story.jpg", media_bytes, "image/jpeg")},
            timeout=120,
        )
        if not response.ok:
            print("DAILY AUTO STORY CLOUDINARY ERROR:", response.text[:500])
            return None, "Cloudinary could not host Daily Auto Story."
        result = response.json()
        secure_url = str(result.get("secure_url", "") or "").strip()
        if not secure_url.startswith("https://"):
            return None, "Cloudinary did not return a HTTPS Story URL."
        return secure_url, ""
    except Exception as error:
        return None, "Daily Auto Story upload failed: " + str(error)


def instagram_archive_posts(limit=25):
    if not INSTAGRAM_ACCESS_TOKEN:
        return []

    try:
        response = requests.get(
            f"https://graph.instagram.com/{API_VERSION}/{INSTAGRAM_USER_ID}/media",
            params={
                "fields": "id,caption,media_type,media_url,thumbnail_url,timestamp,children{id,media_type,media_url,thumbnail_url}",
                "limit": int(limit),
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=30,
        )
        data = response.json()
        if not response.ok or not isinstance(data, dict):
            print("DAILY AUTO STORY MEDIA LOOKUP ERROR:", response.text[:500])
            return []
        return [item for item in data.get("data", []) if isinstance(item, dict)]
    except Exception as error:
        print("DAILY AUTO STORY MEDIA LOOKUP ERROR:", type(error).__name__)
        return []


def _post_timestamp_utc(post):
    value = str((post or {}).get("timestamp", "") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def post_published_today(posts, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_today = now.astimezone(SCHEDULER_TIMEZONE).date()

    for post in posts or []:
        timestamp = _post_timestamp_utc(post)
        if timestamp and timestamp.astimezone(SCHEDULER_TIMEZONE).date() == local_today:
            return True
    return False


def choose_daily_archive_post(posts, stories, now=None, repeat_after_days=7):
    """Choose an older post that was not used by Daily Auto Story recently."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    today_local = now.astimezone(SCHEDULER_TIMEZONE).date()
    cutoff = now - timedelta(days=max(1, int(repeat_after_days or 1)))

    recently_used = set()
    for story in stories or []:
        if not isinstance(story, dict) or story.get("source_type") != "daily_archive":
            continue
        created = _parse_iso_datetime(story.get("created_at"))
        if created and created >= cutoff:
            recently_used.add(str(story.get("source_media_id", "") or ""))

    for post in posts or []:
        if not isinstance(post, dict):
            continue
        media_id = str(post.get("id", "") or "").strip()
        if not media_id or media_id in recently_used:
            continue
        timestamp = _post_timestamp_utc(post)
        if timestamp and timestamp.astimezone(SCHEDULER_TIMEZONE).date() >= today_local:
            continue
        return post

    return None


def _daily_archive_image_url(post):
    if not isinstance(post, dict):
        return ""

    media_type = str(post.get("media_type", "") or "")
    if media_type == "CAROUSEL_ALBUM":
        children = (post.get("children") or {}).get("data", []) if isinstance(post.get("children"), dict) else []
        for child in children:
            if not isinstance(child, dict):
                continue
            if str(child.get("media_type", "") or "") == "IMAGE":
                url = str(child.get("media_url", "") or "").strip()
                if url:
                    return url
        return ""

    if media_type == "IMAGE":
        return str(post.get("media_url", "") or "").strip()

    # For Reels/videos, use a thumbnail so the story text can be overlaid safely.
    return str(post.get("thumbnail_url", "") or "").strip()


def _download_daily_archive_image(url):
    if not url:
        return None
    try:
        response = requests.get(str(url), timeout=60)
        if response.ok and response.content:
            return response.content
    except Exception as error:
        print("DAILY AUTO STORY DOWNLOAD ERROR:", type(error).__name__)
    return None


def _daily_story_gap_ok(stories, now_utc, min_gap_hours):
    gap = timedelta(hours=max(0, int(min_gap_hours or 0)))
    if gap.total_seconds() <= 0:
        return True

    for item in reversed(stories or []):
        if not isinstance(item, dict):
            continue
        reference = _parse_iso_datetime(
            item.get("published_at")
            or item.get("scheduled_for")
            or item.get("created_at")
        )
        if not reference:
            continue
        delta = now_utc - reference
        if timedelta(0) <= delta < gap:
            return False
    return True


def queue_daily_archive_story(now=None, force=False):
    settings = load_auto_story_settings()
    if not force and not settings.get("daily_enabled"):
        return 0

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    now_local = now_utc.astimezone(SCHEDULER_TIMEZONE)

    posts = instagram_archive_posts(25)
    if not posts:
        return 0

    # Daily archive is only a fallback. If a real post was published today,
    # do nothing for the day.
    if not force and post_published_today(posts, now=now_utc):
        return 0

    with STATE_LOCK:
        stories = load_stories()

        # Normal daily runs are limited to one per local day and respect the
        # minimum Story gap. A manual test run bypasses only these timing gates.
        if not force:
            for item in stories:
                if not isinstance(item, dict) or item.get("source_type") != "daily_archive":
                    continue
                created = _parse_iso_datetime(item.get("created_at"))
                if created and created.astimezone(SCHEDULER_TIMEZONE).date() == now_local.date():
                    return 0

            if not _daily_story_gap_ok(
                stories,
                now_utc,
                settings.get("min_gap_hours", AUTO_STORY_MIN_GAP_HOURS),
            ):
                return 0

    candidate = choose_daily_archive_post(
        posts,
        stories,
        now=now_utc,
        repeat_after_days=settings.get("repeat_after_days", AUTO_STORY_REPEAT_AFTER_DAYS),
    )
    if not candidate:
        return 0

    image_url = _daily_archive_image_url(candidate)
    image_bytes = _download_daily_archive_image(image_url)
    if not image_bytes:
        return 0

    comment = generate_daily_story_comment(candidate.get("caption", ""))
    rendered, error_text = render_daily_story_image(image_bytes, comment)
    if not rendered:
        print("DAILY AUTO STORY RENDER ERROR:", error_text)
        return 0

    story_id = uuid.uuid4().hex[:12]
    media_url, error_text = upload_daily_story_bytes_to_cloudinary(story_id, rendered)
    if not media_url:
        print("DAILY AUTO STORY UPLOAD ERROR:", error_text)
        return 0

    source_media_id = str(candidate.get("id", "") or "")
    created_at = datetime.now(timezone.utc).isoformat()
    record = {
        "id": story_id,
        "group_id": "daily-" + uuid.uuid4().hex[:12],
        "source_type": "daily_archive",
        "test_run": bool(force),
        "source_key": source_media_id,
        "source_media_id": source_media_id,
        "story_comment": comment,
        "media_kind": "image",
        "media_url": media_url,
        "status": "scheduled",
        "created_at": created_at,
        "scheduled_for": now_utc.isoformat(),
        "scheduled_local": now_local.isoformat(),
        "scheduled_timezone": "Africa/Casablanca",
        "published_at": None,
        "instagram_media_id": None,
        "error": "",
    }

    with STATE_LOCK:
        stories = load_stories()
        if not force:
            for item in stories:
                if not isinstance(item, dict) or item.get("source_type") != "daily_archive":
                    continue
                created = _parse_iso_datetime(item.get("created_at"))
                if created and created.astimezone(SCHEDULER_TIMEZONE).date() == now_local.date():
                    return 0
        stories.append(record)
        if len(stories) > 1000:
            stories = stories[-1000:]
        save_stories(stories)

    print("Daily Auto Story queued:", source_media_id, comment)
    return 1


def process_daily_auto_story(now=None):
    settings = load_auto_story_settings()
    if not settings.get("daily_enabled"):
        return

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    local = now.astimezone(SCHEDULER_TIMEZONE)
    try:
        hour, minute = [int(value) for value in str(settings.get("daily_time", AUTO_STORY_DAILY_TIME)).split(":", 1)]
    except Exception:
        hour, minute = 20, 0

    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < due:
        return

    queue_daily_archive_story(now=now)


# ============================================================
# DASHBOARD - INSTAGRAM STORIES
# ============================================================

def load_stories():
    data = load_json_file(STORIES_FILE, [])
    return data if isinstance(data, list) else []


def save_stories(items):
    return save_json_file(STORIES_FILE, items if isinstance(items, list) else [])


def build_story_container_payload(media_url, media_kind):
    """Build the Instagram container payload for one Story media item."""
    media_url = str(media_url or "").strip()
    media_kind = str(media_kind or "").strip().lower()
    if media_kind == "image":
        return {"media_type": "STORIES", "image_url": media_url}
    if media_kind == "video":
        return {"media_type": "STORIES", "video_url": media_url}
    return {}


def story_is_due(item, now=None):
    if not isinstance(item, dict) or item.get("status") != "scheduled":
        return False
    scheduled_for = _parse_iso_datetime(item.get("scheduled_for"))
    if not scheduled_for:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return scheduled_for <= now.astimezone(timezone.utc)


def upload_story_media_to_cloudinary(story_id, media_file):
    if not cloudinary_is_configured():
        return None, None, (
            "Cloudinary is not configured. Add CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in Railway Variables."
        )

    filename = secure_filename(str(getattr(media_file, "filename", "") or ""))
    content_type = str(getattr(media_file, "mimetype", "") or "").lower()
    suffix = Path(filename).suffix.lower()

    is_video = content_type.startswith("video/") or suffix in {".mp4", ".mov", ".m4v"}
    media_kind = "video" if is_video else "image"

    try:
        media_bytes = media_file.read()
    except Exception as error:
        return None, None, "Could not read Story media: " + str(error)

    if not media_bytes:
        return None, None, "Story media is empty."

    if media_kind == "image":
        # Browser converts Story images to a normal JPEG before upload.
        if not media_bytes.startswith(b"\xff\xd8\xff"):
            return None, None, "Story images must be JPEG. Select the image again from the dashboard."
        resource_type = "image"
        upload_name = "story.jpg"
        upload_mime = "image/jpeg"
        data = {
            "folder": "imran-prompts/stories",
            "format": "jpg",
            "overwrite": "true",
            "public_id": f"story-{story_id}"
        }
    else:
        if not (content_type in {"video/mp4", "video/quicktime", "video/x-m4v", "application/octet-stream"} or suffix in {".mp4", ".mov", ".m4v"}):
            return None, None, "Story video must be MP4 or MOV."
        resource_type = "video"
        upload_name = filename or "story.mp4"
        upload_mime = content_type if content_type.startswith("video/") else "video/mp4"
        data = {
            "folder": "imran-prompts/stories",
            "overwrite": "true",
            "public_id": f"story-{story_id}"
        }

    upload_url = (
        "https://api.cloudinary.com/v1_1/"
        f"{CLOUDINARY_CLOUD_NAME}/{resource_type}/upload"
    )

    try:
        response = requests.post(
            upload_url,
            auth=(CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET),
            data=data,
            files={"file": (upload_name, media_bytes, upload_mime)},
            timeout=120
        )
        print("Cloudinary Story upload:", response.status_code)
        if not response.ok:
            print(response.text)
            return None, None, "Cloudinary could not host the Story media: " + response.text[:500]
        result = response.json()
        secure_url = str(result.get("secure_url", "") or "").strip()
        if not secure_url.startswith("https://"):
            return None, None, "Cloudinary did not return a public HTTPS Story URL."
        return secure_url, media_kind, ""
    except requests.RequestException as error:
        return None, None, "Cloudinary Story upload request failed: " + str(error)
    except Exception as error:
        return None, None, "Could not prepare Story media for Cloudinary: " + str(error)


def publish_instagram_story_url(media_url, media_kind):
    if not INSTAGRAM_ACCESS_TOKEN:
        return None, "Instagram access token is missing."
    if not INSTAGRAM_USER_ID:
        return None, "Instagram user ID is missing."

    payload = build_story_container_payload(media_url, media_kind)
    if not payload:
        return None, "Unsupported Story media type."

    # Unique query avoids a stale fetch result being reused by Meta.
    media_key = "image_url" if media_kind == "image" else "video_url"
    separator = "&" if "?" in payload[media_key] else "?"
    payload[media_key] = payload[media_key] + separator + "ig_story=" + uuid.uuid4().hex[:10]

    container, error_text = instagram_graph_post(
        f"{INSTAGRAM_USER_ID}/media",
        payload
    )
    if not container or not container.get("id"):
        return None, error_text or "Could not create Instagram Story container."

    creation_id = str(container["id"])
    attempts = 30 if media_kind == "video" else 12
    if not wait_for_instagram_container(creation_id, attempts=attempts, delay_seconds=2):
        return None, "Instagram could not prepare the Story container."

    published, error_text = instagram_graph_post(
        f"{INSTAGRAM_USER_ID}/media_publish",
        {"creation_id": creation_id}
    )
    if not published or not published.get("id"):
        return None, error_text or "Could not publish the Instagram Story."

    return str(published["id"]), ""


def _find_story(items, story_id):
    return next((item for item in items if str(item.get("id", "")) == str(story_id)), None)


@app.route("/api/stories", methods=["GET"])
def get_dashboard_stories():
    with STATE_LOCK:
        stories = load_stories()
        result = [dict(item) for item in reversed(stories[-200:])]
    return {"stories": result}, 200


@app.route("/api/story-create", methods=["POST"])
def create_dashboard_story():
    media_file = request.files.get("media")
    action = str(request.form.get("action", "publish") or "publish").strip().lower()
    scheduled_value = str(request.form.get("scheduled_for", "") or "").strip()

    if media_file is None or not getattr(media_file, "filename", ""):
        return {"error": "Choose one image or video for the Story."}, 400
    if action not in {"publish", "schedule"}:
        return {"error": "Invalid Story action."}, 400

    scheduled_utc = None
    scheduled_local = None
    if action == "schedule":
        scheduled_utc, schedule_error = parse_dashboard_schedule(scheduled_value)
        if not scheduled_utc:
            return {"error": schedule_error}, 400
        scheduled_local = scheduled_utc.astimezone(SCHEDULER_TIMEZONE)

    story_id = uuid.uuid4().hex[:12]
    media_url, media_kind, upload_error = upload_story_media_to_cloudinary(story_id, media_file)
    if not media_url:
        return {"error": upload_error or "Could not host Story media."}, 500

    now_iso = datetime.now(timezone.utc).isoformat()
    story = {
        "id": story_id,
        "media_kind": media_kind,
        "media_url": media_url,
        "status": "scheduled" if action == "schedule" else "publishing",
        "created_at": now_iso,
        "scheduled_for": scheduled_utc.isoformat() if scheduled_utc else None,
        "scheduled_local": scheduled_local.isoformat() if scheduled_local else None,
        "scheduled_timezone": "Africa/Casablanca" if scheduled_local else None,
        "published_at": None,
        "instagram_media_id": None,
        "error": ""
    }

    with STATE_LOCK:
        stories = load_stories()
        stories.append(story)
        if len(stories) > 1000:
            stories = stories[-1000:]
        save_stories(stories)

    if action == "schedule":
        return {"success": True, "story": story}, 200

    instagram_media_id, error_text = publish_instagram_story_url(media_url, media_kind)
    finished_at = datetime.now(timezone.utc).isoformat()

    with STATE_LOCK:
        stories = load_stories()
        current = _find_story(stories, story_id)
        if current:
            if instagram_media_id:
                current["status"] = "published"
                current["published_at"] = finished_at
                current["instagram_media_id"] = instagram_media_id
                current["error"] = ""
            else:
                current["status"] = "failed"
                current["failed_at"] = finished_at
                current["error"] = str(error_text or "Story publishing failed.")[:1000]
            story = dict(current)
            save_stories(stories)

    if not instagram_media_id:
        return {"error": story.get("error") or "Story publishing failed.", "story": story}, 500

    return {"success": True, "story": story}, 200


@app.route("/api/story-cancel", methods=["POST"])
def cancel_dashboard_story():
    payload = request.get_json(silent=True) or {}
    story_id = str(payload.get("story_id", "") or "").strip()
    if not story_id:
        return {"error": "Story ID is required."}, 400

    lock_path = acquire_schedule_lock("story-" + story_id)
    if not lock_path:
        return {"error": "This Story is currently being processed. Try again in a moment."}, 409
    try:
        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)
            if not story:
                return {"error": "Story not found."}, 404
            if story.get("status") != "scheduled":
                return {"error": "This Story is not currently scheduled."}, 409
            story["status"] = "cancelled"
            story["cancelled_at"] = datetime.now(timezone.utc).isoformat()
            story["scheduled_for"] = None
            story["scheduled_local"] = None
            story["scheduled_timezone"] = None
            save_stories(stories)
            return {"success": True, "story": dict(story)}, 200
    finally:
        release_schedule_lock(lock_path)


@app.route("/api/story-retry", methods=["POST"])
def retry_dashboard_story():
    payload = request.get_json(silent=True) or {}
    story_id = str(payload.get("story_id", "") or "").strip()
    if not story_id:
        return {"error": "Story ID is required."}, 400

    lock_path = acquire_schedule_lock("story-" + story_id)
    if not lock_path:
        return {"error": "This Story is currently being processed. Try again in a moment."}, 409
    try:
        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)
            if not story:
                return {"error": "Story not found."}, 404
            if story.get("status") not in {"failed", "cancelled"}:
                return {"error": "Only failed or cancelled Stories can be retried."}, 409
            media_url = str(story.get("media_url", "") or "")
            media_kind = str(story.get("media_kind", "") or "")
            story["status"] = "publishing"
            story["error"] = ""
            story["retry_started_at"] = datetime.now(timezone.utc).isoformat()
            save_stories(stories)

        instagram_media_id, error_text = publish_instagram_story_url(media_url, media_kind)
        finished_at = datetime.now(timezone.utc).isoformat()
        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)
            if story:
                if instagram_media_id:
                    story["status"] = "published"
                    story["published_at"] = finished_at
                    story["instagram_media_id"] = instagram_media_id
                    story["error"] = ""
                else:
                    story["status"] = "failed"
                    story["failed_at"] = finished_at
                    story["error"] = str(error_text or "Story publishing failed.")[:1000]
                result = dict(story)
                save_stories(stories)
            else:
                result = {}
        if not instagram_media_id:
            return {"error": result.get("error") or "Story publishing failed.", "story": result}, 500
        return {"success": True, "story": result}, 200
    finally:
        release_schedule_lock(lock_path)


def publish_scheduled_story(story_id):
    """
    Publish a scheduled Story safely.

    The media URL is stored when the Story is scheduled, but the Instagram
    Story container is created only when publication is due. If Meta returns
    a transient publishing error, retry the complete create/wait/publish cycle
    with a brand-new container instead of reusing an unavailable creation_id.
    """
    lock_path = acquire_schedule_lock("story-" + str(story_id))
    if not lock_path:
        return

    try:
        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)

            if not story or not story_is_due(story):
                return

            story["status"] = "publishing"
            story["publish_started_at"] = datetime.now(timezone.utc).isoformat()
            story["error"] = ""

            media_url = str(story.get("media_url", "") or "").strip()
            media_kind = str(story.get("media_kind", "") or "").strip().lower()

            save_stories(stories)

        instagram_media_id = None
        error_text = ""

        if not media_url:
            error_text = "Scheduled Story media URL is missing."
        elif media_kind not in {"image", "video"}:
            error_text = "Scheduled Story media type is invalid."
        else:
            max_attempts = 3

            for attempt in range(1, max_attempts + 1):
                print(
                    "Scheduled Story publish attempt:",
                    story_id,
                    f"{attempt}/{max_attempts}"
                )

                instagram_media_id, error_text = publish_instagram_story_url(
                    media_url,
                    media_kind
                )

                if instagram_media_id:
                    break

                print(
                    "Scheduled Story publish attempt failed:",
                    story_id,
                    str(error_text or "")[:500]
                )

                if attempt < max_attempts:
                    time.sleep(5)

        finished_at = datetime.now(timezone.utc).isoformat()

        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)

            if not story:
                return

            if instagram_media_id:
                story["status"] = "published"
                story["published_at"] = finished_at
                story["instagram_media_id"] = instagram_media_id
                story["error"] = ""
                story.pop("failed_at", None)
                print("Scheduled Instagram Story published:", story_id, instagram_media_id)
            else:
                story["status"] = "failed"
                story["failed_at"] = finished_at
                story["error"] = str(
                    error_text or "Scheduled Story publishing failed."
                )[:1000]
                print("Scheduled Instagram Story failed:", story_id, story["error"])

            save_stories(stories)

    except Exception as error:
        print("SCHEDULED STORY ERROR:", type(error).__name__)

        with STATE_LOCK:
            stories = load_stories()
            story = _find_story(stories, story_id)

            if story:
                story["status"] = "failed"
                story["failed_at"] = datetime.now(timezone.utc).isoformat()
                story["error"] = str(error)[:1000]
                save_stories(stories)

    finally:
        release_schedule_lock(lock_path)


def process_due_scheduled_stories():
    now = datetime.now(timezone.utc)
    with STATE_LOCK:
        stories = load_stories()
        due_ids = [str(item.get("id", "")) for item in stories if story_is_due(item, now=now)]
    for story_id in due_ids:
        if story_id:
            publish_scheduled_story(story_id)


# ============================================================
# DASHBOARD - INSTAGRAM PUBLISHING
# ============================================================

def build_instagram_caption(draft):

    parts = []

    caption = str(draft.get("caption", "") or "").strip()
    cta = str(draft.get("cta", "") or "").strip()
    hashtags = str(draft.get("hashtags", "") or "").strip()

    if caption:
        parts.append(caption)

    if cta:
        parts.append(cta)

    if hashtags:
        parts.append(hashtags)

    return "\n\n".join(parts).strip()


def instagram_graph_post(path, payload):

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{path.lstrip('/')}"
    )

    data = dict(payload or {})
    data["access_token"] = INSTAGRAM_ACCESS_TOKEN

    response = requests.post(
        url,
        data=data,
        timeout=60
    )

    print(
        "Instagram Graph POST:",
        path,
        response.status_code
    )

    if not response.ok:
        print(response.text)
        return None, response.text

    try:
        return response.json(), ""
    except Exception:
        return None, "Invalid JSON response from Instagram."


def instagram_container_status(creation_id):

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{creation_id}"
    )

    params = {
        "fields": "status_code,status",
        "access_token": INSTAGRAM_ACCESS_TOKEN
    }

    try:
        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        if not response.ok:
            print("Container status error:", response.text)
            return ""

        data = response.json()
        return str(data.get("status_code", "") or "").upper()

    except Exception as error:
        print("CONTAINER STATUS ERROR:", type(error).__name__)
        return ""


def wait_for_instagram_container(creation_id, attempts=10, delay_seconds=2):

    import time

    for _ in range(attempts):

        status = instagram_container_status(creation_id)

        if status in {"FINISHED", "PUBLISHED"}:
            return True

        if status in {"ERROR", "EXPIRED"}:
            return False

        time.sleep(delay_seconds)

    # Image containers are often publishable before status is returned.
    return True


def publish_instagram_draft(draft, public_base_url):

    # Posts are intentionally manual-publish only. Keep this guard at the
    # lowest level so an accidental internal call cannot publish a Post.
    del draft, public_base_url
    return None, (
        "Post auto-publishing is disabled. Use the Post Assistant, then "
        "publish manually in Instagram."
    )

    # Keep the old argument for compatibility, but do not use Railway
    # as Instagram's media host. Meta's crawler can be blocked by the
    # Railway edge even when the URL opens normally in a browser.
    del public_base_url

    if not INSTAGRAM_ACCESS_TOKEN:
        return None, "Instagram access token is missing."

    if not INSTAGRAM_USER_ID:
        return None, "Instagram user ID is missing."

    if not cloudinary_is_configured():
        return None, (
            "Cloudinary is not configured. Add CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET in Railway Variables."
        )

    images = draft.get("images", [])

    if not isinstance(images, list) or not images:
        return None, "This draft has no images."

    caption = build_instagram_caption(draft)
    draft_id = str(draft.get("id", "") or "").strip()

    if not draft_id:
        return None, "Draft ID is missing."

    image_urls = []

    for image_number, relative_path in enumerate(images[:10], start=1):

        relative_path = str(relative_path or "").strip()

        if not relative_path:
            continue

        local_path = DATA_DIR / relative_path

        try:
            resolved_path = local_path.resolve()
            resolved_data_dir = DATA_DIR.resolve()

            if resolved_data_dir not in resolved_path.parents:
                return None, "Invalid draft image path."
        except Exception:
            return None, "Invalid draft image path."

        media_url, upload_error = upload_draft_image_to_cloudinary(
            draft_id,
            image_number,
            resolved_path
        )

        if not media_url:
            return None, upload_error or "Could not host the draft image."

        # Unique URL per attempt prevents Meta from reusing a previous
        # failed fetch result.
        separator = "&" if "?" in media_url else "?"
        media_url = (
            media_url
            + separator
            + "ig_publish="
            + uuid.uuid4().hex[:10]
        )

        print("Instagram hosted media URL:", media_url)
        image_urls.append(media_url)

    if not image_urls:
        return None, "No publishable images were found."

    if len(image_urls) == 1:

        container, error_text = instagram_graph_post(
            f"{INSTAGRAM_USER_ID}/media",
            {
                "image_url": image_urls[0],
                "caption": caption
            }
        )

        if not container or not container.get("id"):
            return None, error_text or "Could not create Instagram media container."

        creation_id = str(container["id"])

        if not wait_for_instagram_container(creation_id):
            return None, "Instagram could not prepare the image container."

        published, error_text = instagram_graph_post(
            f"{INSTAGRAM_USER_ID}/media_publish",
            {"creation_id": creation_id}
        )

        if not published or not published.get("id"):
            return None, error_text or "Could not publish the Instagram post."

        return str(published["id"]), ""

    child_ids = []

    for image_url in image_urls[:10]:

        child, error_text = instagram_graph_post(
            f"{INSTAGRAM_USER_ID}/media",
            {
                "image_url": image_url,
                "is_carousel_item": "true"
            }
        )

        if not child or not child.get("id"):
            return None, error_text or "Could not create a carousel item."

        child_id = str(child["id"])

        if not wait_for_instagram_container(child_id):
            return None, "Instagram could not prepare a carousel image."

        child_ids.append(child_id)

    if len(child_ids) < 2:
        return None, "Instagram carousel requires at least 2 images."

    parent, error_text = instagram_graph_post(
        f"{INSTAGRAM_USER_ID}/media",
        {
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption
        }
    )

    if not parent or not parent.get("id"):
        return None, error_text or "Could not create the carousel container."

    parent_id = str(parent["id"])

    if not wait_for_instagram_container(parent_id):
        return None, "Instagram could not prepare the carousel container."

    published, error_text = instagram_graph_post(
        f"{INSTAGRAM_USER_ID}/media_publish",
        {"creation_id": parent_id}
    )

    if not published or not published.get("id"):
        return None, error_text or "Could not publish the Instagram carousel."

    return str(published["id"]), ""


@app.route("/api/publish-draft", methods=["POST"])
def publish_dashboard_draft():
    return {
        "error": "Post auto-publishing is disabled. Use the Post Assistant, then publish manually in Instagram."
    }, 410

def parse_dashboard_schedule(value):
    """Convert the dashboard datetime-local value from Morocco time to UTC."""
    raw_value = str(value or "").strip()
    if not raw_value:
        return None, "Choose a publish date and time."

    try:
        scheduled_local = datetime.fromisoformat(raw_value)
    except ValueError:
        return None, "Invalid publish date and time."

    if scheduled_local.tzinfo is None:
        scheduled_local = scheduled_local.replace(tzinfo=SCHEDULER_TIMEZONE)
    else:
        scheduled_local = scheduled_local.astimezone(SCHEDULER_TIMEZONE)

    scheduled_utc = scheduled_local.astimezone(timezone.utc)
    if scheduled_utc <= datetime.now(timezone.utc):
        return None, "The scheduled time must be in the future."

    return scheduled_utc, ""


def acquire_schedule_lock(draft_id):
    """Cross-process lock so multiple Railway workers cannot publish twice."""
    safe_id = secure_filename(str(draft_id or ""))
    if not safe_id:
        return None

    lock_path = SCHEDULER_LOCKS_DIR / f"{safe_id}.lock"

    try:
        if lock_path.exists():
            age_seconds = time.time() - lock_path.stat().st_mtime
            if age_seconds > 900:
                lock_path.unlink(missing_ok=True)
    except Exception:
        pass

    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, datetime.now(timezone.utc).isoformat().encode("utf-8"))
        os.close(descriptor)
        return lock_path
    except FileExistsError:
        return None
    except Exception as error:
        print("SCHEDULE LOCK ERROR:", type(error).__name__)
        return None


def release_schedule_lock(lock_path):
    if not lock_path:
        return
    try:
        Path(lock_path).unlink(missing_ok=True)
    except Exception as error:
        print("SCHEDULE LOCK RELEASE ERROR:", type(error).__name__)


@app.route("/api/schedule-draft", methods=["POST"])
def schedule_dashboard_draft():
    return {
        "error": "Post auto-publishing is disabled. Use the Post Assistant, then publish manually in Instagram."
    }, 410

@app.route("/api/cancel-schedule", methods=["POST"])
def cancel_dashboard_schedule():
    payload = request.get_json(silent=True) or {}
    draft_id = str(payload.get("draft_id", "") or "").strip()

    if not draft_id:
        return {"error": "Draft ID is required."}, 400

    lock_path = acquire_schedule_lock(draft_id)
    if not lock_path:
        return {
            "error": "This scheduled post is currently being processed. Try again in a moment."
        }, 409

    try:
        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            draft = next(
                (item for item in drafts if str(item.get("id", "")) == draft_id),
                None
            )

            if not draft:
                return {"error": "Draft not found."}, 404

            if draft.get("status") == "published":
                return {
                    "error": "This draft has already been published.",
                    "instagram_media_id": draft.get("instagram_media_id")
                }, 409

            if draft.get("status") != "scheduled":
                return {"error": "This draft is not currently scheduled."}, 409

            draft["status"] = "approved"
            draft["scheduled_for"] = None
            draft["scheduled_local"] = None
            draft["scheduled_timezone"] = None
            draft["schedule_error"] = None
            draft["schedule_cancelled_at"] = datetime.now(timezone.utc).isoformat()

            save_json_file(DRAFTS_FILE, drafts)

        print("Dashboard schedule cancelled:", draft_id)
        return {
            "success": True,
            "draft_id": draft_id,
            "status": "approved"
        }, 200

    finally:
        release_schedule_lock(lock_path)


def publish_scheduled_draft(draft_id):
    """Publish one due draft using the same proven Cloudinary/Instagram flow."""
    lock_path = acquire_schedule_lock(draft_id)
    if not lock_path:
        return

    try:
        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            draft = next(
                (item for item in drafts if str(item.get("id", "")) == str(draft_id)),
                None
            )

            if not draft or draft.get("status") != "scheduled":
                return

            scheduled_for = str(draft.get("scheduled_for", "") or "").strip()
            try:
                due_at = datetime.fromisoformat(scheduled_for)
            except ValueError:
                draft["status"] = "schedule_failed"
                draft["schedule_error"] = "Invalid stored schedule time."
                save_json_file(DRAFTS_FILE, drafts)
                return

            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at > datetime.now(timezone.utc):
                return

            draft["status"] = "publishing"
            draft["publish_started_at"] = datetime.now(timezone.utc).isoformat()
            draft["schedule_error"] = None
            save_json_file(DRAFTS_FILE, drafts)
            draft_copy = dict(draft)

        instagram_media_id, error_text = publish_instagram_draft(draft_copy, "")
        finished_at = datetime.now(timezone.utc).isoformat()

        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            for item in drafts:
                if str(item.get("id", "")) != str(draft_id):
                    continue

                if instagram_media_id:
                    item["status"] = "published"
                    item["published_at"] = finished_at
                    item["instagram_media_id"] = instagram_media_id
                    item["schedule_error"] = None
                    print("Scheduled Instagram post published:", draft_id, instagram_media_id)
                else:
                    item["status"] = "schedule_failed"
                    item["schedule_error"] = (
                        error_text or "Scheduled Instagram publishing failed."
                    )[:1000]
                    item["schedule_failed_at"] = finished_at
                    print("Scheduled Instagram post failed:", draft_id, item["schedule_error"])
                break

            save_json_file(DRAFTS_FILE, drafts)

    except Exception as error:
        print("SCHEDULED PUBLISH ERROR:", type(error).__name__)
        with STATE_LOCK:
            drafts = load_dashboard_drafts()
            for item in drafts:
                if str(item.get("id", "")) == str(draft_id):
                    item["status"] = "schedule_failed"
                    item["schedule_error"] = str(error)[:1000]
                    item["schedule_failed_at"] = datetime.now(timezone.utc).isoformat()
                    break
            save_json_file(DRAFTS_FILE, drafts)
    finally:
        release_schedule_lock(lock_path)


def process_due_scheduled_posts():
    now = datetime.now(timezone.utc)
    with STATE_LOCK:
        drafts = load_dashboard_drafts()
        due_ids = []
        for draft in drafts:
            if draft.get("status") != "scheduled":
                continue
            scheduled_for = str(draft.get("scheduled_for", "") or "").strip()
            if not scheduled_for:
                continue
            try:
                due_at = datetime.fromisoformat(scheduled_for)
            except ValueError:
                continue
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            if due_at <= now:
                due_ids.append(str(draft.get("id", "")))

    for draft_id in due_ids:
        if draft_id:
            publish_scheduled_draft(draft_id)


def process_due_tenant_content():
    from database.content_publishing import execute_due_scheduled_content
    factory = _oauth_session_factory()
    if factory is not None:
        execute_due_scheduled_content(factory)


_oauth_maintenance_last_run = 0
_trial_reminders_last_run = 0


def process_trial_reminders():
    """Hourly trial-reminder pass (day 45 / day 55 of the 60-day trial).

    Same hourly-throttle shape as process_oauth_maintenance. The sender
    is the existing stdlib-SMTP path in auth.send_trial_reminder_email;
    when SMTP is unconfigured it returns False, the reminder is counted
    as due-but-unsent (never marked sent) and retried on the next pass.
    """
    global _trial_reminders_last_run
    if time.monotonic() - _trial_reminders_last_run < 3600:
        return
    factory = _oauth_session_factory()
    if factory is not None:
        from database.billing import send_trial_reminders
        result = send_trial_reminders(factory, sender=send_trial_reminder_email)
        if result["sent"] or result["due_unsent"]:
            print(
                "TRIAL REMINDERS:",
                f"sent={result['sent']} due_unsent={result['due_unsent']} "
                f"checked={result['checked']}",
            )
    _trial_reminders_last_run = time.monotonic()


def process_oauth_maintenance():
    global _oauth_maintenance_last_run
    if time.monotonic() - _oauth_maintenance_last_run < 3600:
        return
    factory = _oauth_session_factory()
    if factory is not None:
        from database.token_refresh import refresh_expiring_tokens
        from billing import reconcile_subscriptions
        refresh_expiring_tokens(factory)
        reconcile_subscriptions(factory)
    _oauth_maintenance_last_run = time.monotonic()


def dashboard_scheduler_loop():
    print("Dashboard scheduler: ON", f"({SCHEDULER_POLL_SECONDS}s, Africa/Casablanca)")
    while True:
        # Keep legacy posts manual-only; tenant image posts use durable DB claims.
        for job in (process_oauth_maintenance, process_trial_reminders, process_daily_auto_story,
                    process_due_scheduled_stories, process_due_followups,
                    process_due_oauth_webhook_work, process_due_oauth_followups,
                    process_due_tenant_content):
            try:
                job()
            except Exception as error:
                print("DASHBOARD SCHEDULER ERROR:", job.__name__, type(error).__name__)
        time.sleep(SCHEDULER_POLL_SECONDS)


def start_dashboard_scheduler():
    thread = threading.Thread(
        target=dashboard_scheduler_loop,
        name="dashboard-scheduler",
        daemon=True
    )
    thread.start()


if os.getenv("POSTENZA_EMBEDDED_SCHEDULER", "true").lower() == "true":
    start_dashboard_scheduler()



# ============================================================
# V0.1 SYSTEM / END-TO-END READINESS CHECK
# ============================================================

@app.route("/api/v01-health", methods=["GET"])
def get_v01_health():
    """Return a non-destructive V0.1 readiness snapshot.

    This endpoint never sends Instagram messages, publishes content, or mutates
    lead/follow-up state. It is intentionally read-only apart from a tiny
    write/delete probe used to verify that the persistent data directory is
    writable.
    """

    checks = []

    def add_check(key, label, status, detail, critical=True):
        checks.append({
            "key": str(key),
            "label": str(label),
            "status": str(status),
            "detail": str(detail or ""),
            "critical": bool(critical),
        })

    # Persistent storage: V0.1 depends on Railway's mounted /app/data volume.
    storage_ok = False
    probe_path = DATA_DIR / ".v01_health_probe"
    try:
        probe_path.write_text("ok", encoding="utf-8")
        storage_ok = probe_path.read_text(encoding="utf-8") == "ok"
    except Exception as error:
        add_check(
            "storage",
            "Persistent storage",
            "fail",
            f"Data directory is not writable: {error}",
            True,
        )
    finally:
        try:
            probe_path.unlink(missing_ok=True)
        except Exception:
            pass

    if storage_ok:
        add_check(
            "storage",
            "Persistent storage",
            "pass",
            f"Writable data directory: {DATA_DIR}",
            True,
        )

    # Scheduler thread: required for scheduled posts and delayed follow-ups.
    scheduler_alive = any(
        thread.name == "dashboard-scheduler" and thread.is_alive()
        for thread in threading.enumerate()
    )
    add_check(
        "scheduler",
        "Background scheduler",
        "pass" if scheduler_alive else "fail",
        "Scheduled publishing and follow-up worker is running."
        if scheduler_alive
        else "dashboard-scheduler thread is not running.",
        True,
    )

    # Duplicate protection / cooldown configuration.
    processed = load_processed_comments()
    processed_count = len(processed) if isinstance(processed, dict) else 0
    add_check(
        "duplicate_guard",
        "Duplicate comment protection",
        "pass",
        f"Comment claim guard active • {processed_count} processed comment IDs stored.",
        True,
    )

    # Core AI/content dependencies (v2: Gemini is vision-only — prompt
    # extraction from images — so the check reports that capability).
    add_check(
        "gemini",
        "Gemini vision (prompt extraction)",
        "pass" if GEMINI_CLIENT is not None else "fail",
        f"Configured model: {GEMINI_MODEL}"
        if GEMINI_CLIENT is not None
        else "Gemini API key/client is missing.",
        True,
    )

    cloudinary_ready = bool(
        CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET
    )
    add_check(
        "cloudinary",
        "Cloudinary media hosting",
        "pass" if cloudinary_ready else "fail",
        "Cloudinary credentials are configured."
        if cloudinary_ready
        else "Cloudinary cloud name, API key or API secret is missing.",
        True,
    )

    # Webhook configuration itself. Meta delivery is verified by the live flow;
    # this check confirms the server has the values required to receive it.
    webhook_configured = bool(VERIFY_TOKEN and INSTAGRAM_ACCESS_TOKEN)
    add_check(
        "webhook",
        "Instagram webhook configuration",
        "pass" if webhook_configured else "fail",
        "Verify token and Instagram access token are configured."
        if webhook_configured
        else "Verify token or Instagram access token is missing.",
        True,
    )

    signature_verification_active = webhook_signature_verification_active()
    add_check(
        "webhook_signature",
        "Webhook POST signature verification",
        "pass" if signature_verification_active else "warn",
        "X-Hub-Signature-256 verification is active."
        if signature_verification_active
        else "Webhook POST signature verification is NOT active; META_APP_SECRET is not configured.",
        False,
    )

    # Read-only live Instagram account check. A transient Meta/network issue is
    # reported as warning so it does not mutate or break the bot.
    instagram_live = False
    instagram_detail = "Instagram access token is missing."
    instagram_account = {}
    if INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID:
        try:
            response = requests.get(
                f"https://graph.instagram.com/{API_VERSION}/{INSTAGRAM_USER_ID}",
                params={
                    "fields": "id,username",
                    "access_token": INSTAGRAM_ACCESS_TOKEN,
                },
                timeout=10,
            )
            try:
                payload = response.json()
            except Exception:
                payload = {}

            if response.ok and isinstance(payload, dict):
                instagram_live = True
                instagram_account = {
                    "id": str(payload.get("id", "") or ""),
                    "username": str(payload.get("username", "") or ""),
                }
                username = instagram_account.get("username") or "connected account"
                instagram_detail = f"Live Instagram API connection OK • @{username}."
            else:
                error_message = ""
                if isinstance(payload, dict):
                    error_message = str((payload.get("error") or {}).get("message", "") or "")
                instagram_detail = error_message or f"Instagram API returned HTTP {response.status_code}."
        except requests.RequestException as error:
            instagram_detail = f"Instagram live check unavailable: {error}"

    add_check(
        "instagram_live",
        "Live Instagram API",
        "pass" if instagram_live else "warn",
        instagram_detail,
        False,
    )

    followups = load_followups()
    followup_summary = {
        "pending": 0,
        "sending": 0,
        "sent": 0,
        "cancelled": 0,
        "failed": 0,
        "expired": 0,
    }
    for item in followups:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "") or "")
        if status in followup_summary:
            followup_summary[status] += 1

    requests_data = load_requests()
    latest_request = None
    if requests_data:
        raw = requests_data[-1]
        if isinstance(raw, dict):
            latest_request = {
                "media_id": str(raw.get("media_id", "") or ""),
                "comment_id": str(raw.get("comment_id", "") or ""),
                "commenter_id": str(raw.get("commenter_id", "") or ""),
                "username": str(raw.get("username", "") or ""),
                "created_at": str(raw.get("created_at", "") or ""),
            }

    drafts = load_dashboard_drafts()
    draft_summary = {
        "approved": 0,
        "scheduled": 0,
        "published": 0,
        "schedule_failed": 0,
    }
    for draft in drafts:
        if not isinstance(draft, dict):
            continue
        status = str(draft.get("status", "") or "")
        if status in draft_summary:
            draft_summary[status] += 1

    anti_spam = {
        "request_cooldown_minutes": REQUEST_COOLDOWN_MINUTES,
        "smart_reply_min_interval_seconds": SMART_REPLY_MIN_INTERVAL_SECONDS,
        "smart_reply_max_per_hour": SMART_REPLY_MAX_PER_HOUR,
        "followup_delay_hours": FOLLOWUP_DELAY_HOURS,
        "followup_max_age_hours": FOLLOWUP_MAX_AGE_HOURS,
        "followup_cooldown_days": FOLLOWUP_COOLDOWN_DAYS,
        "duplicate_comment_guard": True,
    }

    critical_failures = [
        item for item in checks
        if item.get("critical") and item.get("status") == "fail"
    ]
    warnings = [item for item in checks if item.get("status") == "warn"]

    if critical_failures:
        overall = "not_ready"
    elif warnings:
        overall = "attention"
    else:
        overall = "ready"

    return {
        "overall": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "anti_spam": anti_spam,
        "followups": followup_summary,
        "drafts": draft_summary,
        "requests_total": len(requests_data),
        "latest_request": latest_request,
        "instagram_account": instagram_account,
        "note": (
            "This check is read-only and does not send DMs or publish content. "
            "A final live end-to-end test is still required before freezing V0.1."
        ),
    }, 200

# ============================================================
# DASHBOARD
# ============================================================

# Dashboard folders
app.template_folder = "dashboard/templates"
app.static_folder = "dashboard/static"


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Legacy dashboard retired: /dashboard now redirects to /app."""
    return redirect("/app")


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True
    )
