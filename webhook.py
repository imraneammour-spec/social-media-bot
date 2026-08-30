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
from flask import Flask, request, send_file, Response, url_for, render_template
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
    get_oauth_pending_access,
    get_oauth_prompt_post,
    get_oauth_prompt_post_by_public_id,
    oauth_prompt_post,
    oauth_request_on_cooldown,
    oauth_smart_reply_allowed,
    pop_oauth_pending_access,
    record_oauth_request,
    record_oauth_smart_reply,
)
from database.oauth_graph import (
    lookup_oauth_media,
    oauth_engagement_capability_state,
    send_oauth_delayed_followup,
    send_oauth_private_prompt_button,
    send_oauth_prompt_access_button,
    send_oauth_public_comment_reply,
)
from database.oauth_followups import (
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
from database.tenant_automation import oauth_automation_runtime_config
from auth import (
    configure_auth,
    csrf_token,
    get_current_user,
    install_auth_route_guard,
    register_auth_routes,
    verify_meta_signature,
    webhook_signature_verification_active,
)
from tenant_dashboard import register_tenant_dashboard

from google import genai
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
RAILWAY_DATA_DIR = Path("/app/data")
DATA_DIR = (
    RAILWAY_DATA_DIR
    if RAILWAY_DATA_DIR.exists()
    else BASE_DIR / "data"
)
DATA_DIR.mkdir(parents=True, exist_ok=True)

POSTS_FILE = DATA_DIR / "posts.json"
PROCESSED_FILE = DATA_DIR / "processed_comments.json"
PENDING_ACCESS_FILE = DATA_DIR / "pending_access.json"
REQUESTS_FILE = DATA_DIR / "requests.json"
SMART_REPLY_ACTIVITY_FILE = DATA_DIR / "smart_reply_activity.json"
FOLLOWUPS_FILE = DATA_DIR / "followups.json"
STORIES_FILE = DATA_DIR / "stories.json"
AUTO_STORY_SETTINGS_FILE = DATA_DIR / "auto_story_settings.json"
DRAFTS_FILE = DATA_DIR / "dashboard_drafts.json"
DRAFT_IMAGES_DIR = DATA_DIR / "dashboard_drafts"

# Story and follow-up scheduler configuration. Posts remain manual-only.
SCHEDULER_TIMEZONE = ZoneInfo("Africa/Casablanca")
SCHEDULER_POLL_SECONDS = 15
SCHEDULER_LOCKS_DIR = DATA_DIR / "scheduler_locks"
SCHEDULER_LOCKS_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_FILE)

app = Flask(__name__)

# Optional Phase 2 configuration only.  No engine, session, table creation, or
# JSON-to-SQL migration occurs during application startup.
DATABASE_URL = database_url_from_environment()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()

INSTAGRAM_ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY"
)

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

LEGACY_INSTAGRAM_USER_ID = os.getenv("LEGACY_INSTAGRAM_USER_ID", "").strip()
INSTAGRAM_USER_ID = LEGACY_INSTAGRAM_USER_ID

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

# The command is explicit (``flask --app webhook bootstrap-owner``) and lazy:
# registering it neither connects to PostgreSQL nor changes Phase 0 JSON state.
register_database_commands(app, lambda: INSTAGRAM_USER_ID)

API_VERSION = "v25.0"

TRIGGER_WORDS = {"ai", "prompt", "prompts", "send", "access"}
REQUEST_COOLDOWN_MINUTES = 10
SMART_REPLY_MIN_INTERVAL_SECONDS = 15
SMART_REPLY_MAX_PER_HOUR = 20
SMART_REPLY_MAX_LENGTH = 180
FOLLOWUP_DELAY_HOURS = 6
FOLLOWUP_MAX_AGE_HOURS = 23
FOLLOWUP_COOLDOWN_DAYS = 7

AUTO_STORY_DAILY_ENABLED = True
AUTO_STORY_DAILY_TIME = "20:00"
AUTO_STORY_REPEAT_AFTER_DAYS = 7
AUTO_STORY_MIN_GAP_HOURS = 6

PUBLIC_REPLY_MESSAGES = [
    "Sent! Check your DMs ✨",
    "Your prompts are ready 👀 Check your DMs.",
    "Done ✨ I sent you the access in DM.",
    "Check your DMs 🚀 Your AI prompts are waiting."
]

GEMINI_MODEL = "gemini-3.6-flash"

STATE_LOCK = threading.Lock()
MEDIA_LOCKS_LOCK = threading.Lock()
MEDIA_LOCKS = {}


# ==================================================
# STARTUP CHECKS
# ==================================================

print("========================================")
print("Imran Social Media Bot")
print("========================================")

if INSTAGRAM_ACCESS_TOKEN:
    print("Instagram access token: OK")
else:
    print("ERROR: INSTAGRAM_ACCESS_TOKEN missing")

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
# GEMINI CLIENT
# ==================================================

if GEMINI_API_KEY:

    GEMINI_CLIENT = genai.Client(
        api_key=GEMINI_API_KEY
    )

    print(
        f"Gemini model: {GEMINI_MODEL}"
    )

else:

    GEMINI_CLIENT = None


# ==================================================
# BASIC ROUTES
# ==================================================

@app.route("/", methods=["GET"])
def home():

    return (
        "Imran Social Media Bot is running!",
        200
    )


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

def load_json_file(
    path,
    default
):

    if not path.exists():

        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as file:

            return json.load(
                file
            )

    except Exception as error:

        print(
            f"JSON READ ERROR {path.name}:",
            error
        )

        return default


def save_json_file(
    path,
    data
):

    try:

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

        return True

    except Exception as error:

        print(
            f"JSON SAVE ERROR {path.name}:",
            error
        )

        return False


# ==================================================
# PROCESSED COMMENTS
# ==================================================

def load_processed_comments():

    data = load_json_file(
        PROCESSED_FILE,
        {}
    )

    if isinstance(
        data,
        list
    ):

        return {
            str(item): "done"
            for item in data
        }

    if not isinstance(
        data,
        dict
    ):

        return {}

    return data


def claim_comment(
    comment_id
):

    comment_id = str(
        comment_id
    )

    with STATE_LOCK:

        data = load_processed_comments()

        if comment_id in data:

            print(
                "Duplicate comment ignored:",
                comment_id
            )

            return False

        data[comment_id] = "processing"

        # Keep latest 2000 comments
        if len(data) > 2000:

            keys = list(
                data.keys()
            )

            for key in keys[:-2000]:

                data.pop(
                    key,
                    None
                )

        save_json_file(
            PROCESSED_FILE,
            data
        )

    return True


def mark_comment_done(
    comment_id
):

    comment_id = str(
        comment_id
    )

    with STATE_LOCK:

        data = load_processed_comments()

        data[comment_id] = "done"

        save_json_file(
            PROCESSED_FILE,
            data
        )


def release_comment(
    comment_id
):

    comment_id = str(
        comment_id
    )

    with STATE_LOCK:

        data = load_processed_comments()

        data.pop(
            comment_id,
            None
        )

        save_json_file(
            PROCESSED_FILE,
            data
        )


# ==================================================
# POSTS DATABASE
# ==================================================

def load_posts():

    with STATE_LOCK:

        data = load_json_file(
            POSTS_FILE,
            {}
        )

        if not isinstance(
            data,
            dict
        ):

            return {}

        return data


def get_saved_post(
    media_id
):

    data = load_posts()

    return data.get(
        str(media_id)
    )


def save_post(
    media_id,
    permalink,
    prompts,
    cover_url=""
):

    with STATE_LOCK:

        data = load_json_file(
            POSTS_FILE,
            {}
        )

        data[str(media_id)] = {

            "media_id":
                str(media_id),

            "permalink":
                permalink or "",

            "cover_url":
                cover_url or "",

            "prompts":
                prompts,

            "created_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),

            "prompt_count":
                len(prompts)
        }

        save_json_file(
            POSTS_FILE,
            data
        )



# ==================================================
# REQUEST ANALYTICS + ANTI-SPAM
# ==================================================

def load_requests():

    data = load_json_file(
        REQUESTS_FILE,
        []
    )

    if not isinstance(data, list):
        return []

    return data


def request_identity(
    media_id,
    commenter_id,
    username
):

    user_part = (
        str(commenter_id)
        if commenter_id
        else str(username or "").strip().lower()
    )

    return f"{media_id}:{user_part}"


def is_request_on_cooldown(
    media_id,
    commenter_id,
    username
):

    key = request_identity(
        media_id,
        commenter_id,
        username
    )

    if key.endswith(":"):
        return False

    now = datetime.now(timezone.utc)

    with STATE_LOCK:

        requests_data = load_requests()

        for item in reversed(requests_data):

            if item.get("request_key") != key:
                continue

            created_at = item.get("created_at")

            if not created_at:
                return False

            try:
                previous = datetime.fromisoformat(created_at)
            except Exception:
                return False

            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)

            elapsed_seconds = (
                now - previous
            ).total_seconds()

            cooldown_seconds = (
                REQUEST_COOLDOWN_MINUTES
                * 60
            )

            return elapsed_seconds < cooldown_seconds

    return False


def log_prompt_request(
    media_id,
    comment_id,
    commenter_id,
    username,
    page_url
):

    entry = {
        "request_key": request_identity(
            media_id,
            commenter_id,
            username
        ),
        "media_id": str(media_id or ""),
        "comment_id": str(comment_id or ""),
        "commenter_id": str(commenter_id or ""),
        "username": str(username or ""),
        "page_url": str(page_url or ""),
        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    with STATE_LOCK:

        data = load_requests()

        data.append(entry)

        if len(data) > 5000:
            data = data[-5000:]

        save_json_file(
            REQUESTS_FILE,
            data
        )


# ==================================================
# AUTO FOLLOW-UP
# ==================================================

def load_followups():
    data = load_json_file(FOLLOWUPS_FILE, [])
    return data if isinstance(data, list) else []


def _parse_iso_datetime(value):
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def followup_due(item, now=None):
    """Return True only inside the safe V1 follow-up window."""
    if not isinstance(item, dict) or item.get("status") != "pending":
        return False
    created_at = _parse_iso_datetime(item.get("created_at"))
    if not created_at:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_hours = (now.astimezone(timezone.utc) - created_at).total_seconds() / 3600
    return FOLLOWUP_DELAY_HOURS <= age_hours < FOLLOWUP_MAX_AGE_HOURS


def schedule_prompt_followup(recipient_id, page_url=""):
    """Queue one follow-up after a user explicitly requested prompt access.

    V1 anti-spam rules: at most one pending follow-up per person, and no new
    follow-up if one was already sent in the last seven days.
    """
    recipient_id = str(recipient_id or "").strip()
    if not recipient_id:
        return False

    now = datetime.now(timezone.utc)
    with STATE_LOCK:
        data = load_followups()

        for item in data:
            if str(item.get("recipient_id", "")) != recipient_id:
                continue
            if item.get("status") == "pending":
                item["created_at"] = now.isoformat()
                item["page_url"] = str(page_url or "")
                save_json_file(FOLLOWUPS_FILE, data)
                return True
            if item.get("status") == "sent":
                sent_at = _parse_iso_datetime(item.get("sent_at"))
                if sent_at and (now - sent_at).total_seconds() < FOLLOWUP_COOLDOWN_DAYS * 86400:
                    return False

        data.append({
            "id": uuid.uuid4().hex[:12],
            "recipient_id": recipient_id,
            "page_url": str(page_url or ""),
            "status": "pending",
            "created_at": now.isoformat(),
            "sent_at": None,
            "cancelled_at": None,
            "error": ""
        })
        if len(data) > 2000:
            data = data[-2000:]
        return save_json_file(FOLLOWUPS_FILE, data)


def cancel_pending_followups(recipient_id, reason="user_replied"):
    recipient_id = str(recipient_id or "").strip()
    if not recipient_id:
        return
    changed = False
    with STATE_LOCK:
        data = load_followups()
        for item in data:
            if str(item.get("recipient_id", "")) == recipient_id and item.get("status") == "pending":
                item["status"] = "cancelled"
                item["cancelled_at"] = datetime.now(timezone.utc).isoformat()
                item["cancel_reason"] = str(reason or "")
                changed = True
        if changed:
            save_json_file(FOLLOWUPS_FILE, data)


def send_followup_message(recipient_id):
    if not INSTAGRAM_ACCESS_TOKEN or not recipient_id:
        return False, "Instagram access token or recipient is missing."

    url = f"https://graph.instagram.com/{API_VERSION}/{INSTAGRAM_USER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {INSTAGRAM_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "recipient": {"id": str(recipient_id)},
        "messaging_type": "RESPONSE",
        "message": {
            "text": (
                "Hope the prompts helped ✨ If you want another set, "
                "comment AI on any post and I’ll send it to you."
            )
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        if response.ok:
            return True, ""
        return False, response.text[:500]
    except requests.RequestException as error:
        return False, str(error)[:500]


def process_due_followups(now=None):
    """Send due follow-ups once; expire anything outside the 23-hour window."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    with STATE_LOCK:
        data = load_followups()
        due_ids = []
        changed = False
        for item in data:
            if item.get("status") != "pending":
                continue
            created_at = _parse_iso_datetime(item.get("created_at"))
            if not created_at:
                item["status"] = "expired"
                item["error"] = "Invalid created_at"
                changed = True
                continue
            age_hours = (now - created_at).total_seconds() / 3600
            if age_hours >= FOLLOWUP_MAX_AGE_HOURS:
                item["status"] = "expired"
                item["expired_at"] = now.isoformat()
                changed = True
            elif age_hours >= FOLLOWUP_DELAY_HOURS:
                due_ids.append(str(item.get("id", "")))
        if changed:
            save_json_file(FOLLOWUPS_FILE, data)

    for followup_id in due_ids:
        with STATE_LOCK:
            data = load_followups()
            item = next((x for x in data if str(x.get("id", "")) == followup_id), None)
            if not item or not followup_due(item, now=now):
                continue
            recipient_id = str(item.get("recipient_id", ""))
            item["status"] = "sending"
            save_json_file(FOLLOWUPS_FILE, data)

        success, error_text = send_followup_message(recipient_id)

        with STATE_LOCK:
            data = load_followups()
            item = next((x for x in data if str(x.get("id", "")) == followup_id), None)
            if not item:
                continue
            if success:
                item["status"] = "sent"
                item["sent_at"] = datetime.now(timezone.utc).isoformat()
                item["error"] = ""
            else:
                # Do not loop aggressively on API failures. Keep the item visible
                # for diagnosis and require a new qualifying request later.
                item["status"] = "failed"
                item["failed_at"] = datetime.now(timezone.utc).isoformat()
                item["error"] = str(error_text or "Follow-up failed")[:500]
            save_json_file(FOLLOWUPS_FILE, data)


# ==================================================
# DASHBOARD - LEADS / PROMPT REQUESTS
# ==================================================

def aggregate_lead_profiles(leads):
    """Group prompt requests into one lightweight profile per Instagram person.

    Temperature is intentionally simple for V1: 1 request = new, 2 = warm,
    3+ = hot. It is an engagement signal, not a claim that a person will buy.
    """
    grouped = {}

    for lead in leads or []:
        if not isinstance(lead, dict):
            continue

        commenter_id = str(lead.get("commenter_id", "") or "").strip()
        username = str(lead.get("username", "") or "").strip()
        identity = commenter_id or username.lower()
        if not identity:
            continue

        created_at = str(lead.get("created_at", "") or "")
        profile = grouped.get(identity)
        if profile is None:
            profile = {
                "commenter_id": commenter_id,
                "username": username,
                "request_count": 0,
                "delivered_count": 0,
                "waiting_count": 0,
                "first_activity": created_at,
                "last_activity": created_at,
                "latest_media_id": "",
                "latest_post_caption": "",
                "latest_post_thumbnail_url": "",
                "latest_post_media_url": "",
                "latest_post_permalink": "",
                "latest_page_url": "",
            }
            grouped[identity] = profile

        profile["request_count"] += 1
        if lead.get("status") == "waiting_confirmation":
            profile["waiting_count"] += 1
        else:
            profile["delivered_count"] += 1

        if username:
            profile["username"] = username
        if commenter_id:
            profile["commenter_id"] = commenter_id

        if not profile["first_activity"] or (created_at and created_at < profile["first_activity"]):
            profile["first_activity"] = created_at

        is_latest = not profile["last_activity"] or (created_at and created_at >= profile["last_activity"] )
        if is_latest:
            profile["last_activity"] = created_at
            profile["latest_media_id"] = str(lead.get("media_id", "") or "")
            profile["latest_post_caption"] = str(lead.get("post_caption", "") or "")
            profile["latest_post_thumbnail_url"] = str(lead.get("post_thumbnail_url", "") or "")
            profile["latest_post_media_url"] = str(lead.get("post_media_url", "") or "")
            profile["latest_post_permalink"] = str(lead.get("post_permalink", "") or "")
            profile["latest_page_url"] = str(lead.get("page_url", "") or "")

    profiles = []
    for profile in grouped.values():
        count = profile["request_count"]
        if count >= 3:
            temperature = "hot"
        elif count == 2:
            temperature = "warm"
        else:
            temperature = "new"
        profile["temperature"] = temperature
        profiles.append(profile)

    rank = {"hot": 3, "warm": 2, "new": 1}
    profiles.sort(
        key=lambda item: (
            rank.get(item.get("temperature"), 0),
            item.get("request_count", 0),
            item.get("last_activity", ""),
        ),
        reverse=True,
    )
    return profiles


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
    # generated it without making one API request per row.
    media_map = {}
    if INSTAGRAM_ACCESS_TOKEN:
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
        except Exception as error:
            print("LEADS MEDIA LOOKUP ERROR:", error)

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


# ==================================================
# SMART COMMENT REPLIES
# ==================================================

def should_smart_reply(comment_text):

    text = str(comment_text or "").strip()

    if not text:
        return False

    words = re.findall(
        r"\b\w+\b",
        text.lower()
    )

    if any(
        word in TRIGGER_WORDS
        for word in words
    ):
        return False

    meaningful = re.sub(
        r"[^\w]+",
        "",
        text,
        flags=re.UNICODE
    )

    # Skip emoji-only / tiny-noise comments.
    if len(meaningful) < 2:
        return False

    return True


def load_smart_reply_activity():

    data = load_json_file(
        SMART_REPLY_ACTIVITY_FILE,
        []
    )

    if not isinstance(data, list):
        return []

    return data


def smart_reply_rate_allowed():

    now = datetime.now(timezone.utc)
    one_hour_ago = now.timestamp() - 3600

    with STATE_LOCK:

        data = load_smart_reply_activity()
        recent = []

        for item in data:

            created_at = item.get("created_at")

            if not created_at:
                continue

            try:
                dt = datetime.fromisoformat(created_at)
            except Exception:
                continue

            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            if dt.timestamp() >= one_hour_ago:
                recent.append(item)

        if len(recent) >= SMART_REPLY_MAX_PER_HOUR:
            return False

        if recent:

            last_created = recent[-1].get("created_at")

            try:
                last_dt = datetime.fromisoformat(last_created)

                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)

                elapsed = (
                    now - last_dt
                ).total_seconds()

                if elapsed < SMART_REPLY_MIN_INTERVAL_SECONDS:
                    return False

            except Exception:
                pass

    return True


def record_smart_reply(
    comment_id,
    commenter_id,
    username,
    reply_text
):

    entry = {
        "comment_id": str(comment_id or ""),
        "commenter_id": str(commenter_id or ""),
        "username": str(username or ""),
        "reply_text": str(reply_text or ""),
        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    with STATE_LOCK:

        data = load_smart_reply_activity()
        data.append(entry)

        # Keep only the latest 1000 successful smart replies.
        if len(data) > 1000:
            data = data[-1000:]

        save_json_file(
            SMART_REPLY_ACTIVITY_FILE,
            data
        )


def generate_smart_reply(
    comment_text,
    username=""
):

    if GEMINI_CLIENT is None:
        return ""

    source_text = str(
        comment_text or ""
    ).strip()

    if not source_text:
        return ""

    instructions = f"""
You are replying as the owner of the Instagram account @imran.prompts.

Write ONE short, natural Instagram comment reply to the user's comment below.

Rules:
- Reply in the same language as the comment whenever possible.
- Keep it friendly, casual and human-sounding.
- Maximum 1 short sentence, ideally under 120 characters.
- Never mention that you are an AI, bot or automation.
- Do not use hashtags or links.
- Do not ask the user to DM unless their comment clearly asks for more information.
- Do not invent facts, prices, personal details, project details or promises.
- If the comment asks for specific information that is not present, answer politely without making up details.
- Avoid repeating the exact wording of the user's comment.
- Use at most one emoji, and only when it feels natural.
- Do not use salesy language.
- Return ONLY the reply text, nothing else.

Username: @{username or 'visitor'}
Comment: {source_text}
"""

    try:

        response = (
            GEMINI_CLIENT
            .models
            .generate_content(

                model=GEMINI_MODEL,

                contents=instructions,

                config=
                    types.GenerateContentConfig(

                        temperature=0.7
                    )
            )
        )

        reply = str(
            response.text or ""
        ).strip()

        reply = re.sub(
            r"\s+",
            " ",
            reply
        ).strip()

        reply = reply.strip(
            '"\''
        )

        if len(reply) > SMART_REPLY_MAX_LENGTH:
            reply = reply[:SMART_REPLY_MAX_LENGTH].rstrip()

        return reply

    except Exception as error:

        print(
            "SMART REPLY GEMINI ERROR:",
            error
        )

        return ""


def send_comment_reply_text(
    comment_id,
    message
):

    if (
        not INSTAGRAM_ACCESS_TOKEN
        or not comment_id
        or not message
    ):
        return False

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{comment_id}/replies"
    )

    payload = {
        "message": str(message).strip(),
        "access_token": INSTAGRAM_ACCESS_TOKEN
    }

    try:

        response = requests.post(
            url,
            data=payload,
            timeout=30
        )

        print(
            "Comment Reply:",
            response.status_code
        )

        if not response.ok:
            print(response.text)

        return response.ok

    except requests.RequestException as error:

        print(
            "COMMENT REPLY ERROR:",
            error
        )

        return False


def process_smart_comment(
    comment_id,
    comment_text,
    commenter_id,
    username
):

    if not should_smart_reply(
        comment_text
    ):
        return

    if not claim_comment(
        comment_id
    ):
        return

    try:

        if not smart_reply_rate_allowed():

            print(
                "Smart reply skipped by rate limit."
            )

            mark_comment_done(
                comment_id
            )

            return

        reply_text = generate_smart_reply(
            comment_text,
            username
        )

        if not reply_text:

            print(
                "No smart reply generated."
            )

            mark_comment_done(
                comment_id
            )

            return

        success = send_comment_reply_text(
            comment_id,
            reply_text
        )

        if success:

            record_smart_reply(
                comment_id,
                commenter_id,
                username,
                reply_text
            )

            mark_comment_done(
                comment_id
            )

            print(
                "Smart comment reply sent successfully."
            )

        else:

            release_comment(
                comment_id
            )

    except Exception as error:

        print(
            "SMART COMMENT PROCESS ERROR:",
            error
        )

        release_comment(
            comment_id
        )


# ==================================================
# MEDIA LOCK
# ==================================================

def get_media_lock(
    media_id
):

    media_id = str(
        media_id
    )

    with MEDIA_LOCKS_LOCK:

        if media_id not in MEDIA_LOCKS:

            MEDIA_LOCKS[
                media_id
            ] = threading.Lock()

        return MEDIA_LOCKS[
            media_id
        ]


# ==================================================
# INSTAGRAM MEDIA LOOKUP
# ==================================================

def get_instagram_media(
    media_id
):

    if not INSTAGRAM_ACCESS_TOKEN:

        return None

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{media_id}"
    )

    params = {

        "fields": (
            "id,"
            "media_type,"
            "media_url,"
            "permalink,"
            "children{"
            "id,"
            "media_type,"
            "media_url"
            "}"
        ),

        "access_token":
            INSTAGRAM_ACCESS_TOKEN
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

        print(
            "Media lookup:",
            response.status_code
        )

        if response.ok:

            return response.json()

        print(
            response.text
        )

        return None

    except requests.RequestException as error:

        print(
            "MEDIA LOOKUP ERROR:",
            error
        )

        return None


# ==================================================
# CAROUSEL ITEMS
# ==================================================

def get_carousel_items(
    media_info
):

    if not media_info:

        return []

    media_type = media_info.get(
        "media_type",
        ""
    )

    if (
        media_type
        !=
        "CAROUSEL_ALBUM"
    ):

        media_url = media_info.get(
            "media_url"
        )

        if not media_url:

            return []

        return [

            {

                "id":
                    media_info.get(
                        "id"
                    ),

                "media_type":
                    media_type,

                "media_url":
                    media_url
            }

        ]

    children = media_info.get(
        "children",
        {}
    )

    return children.get(
        "data",
        []
    )


# ==================================================
# DOWNLOAD IMAGE
# ==================================================

def download_image_bytes(
    image_url
):

    try:

        response = requests.get(
            image_url,
            timeout=40
        )

        if not response.ok:

            print(
                "Image download failed:",
                response.status_code
            )

            return None

        mime_type = (
            response.headers.get(
                "Content-Type",
                "image/jpeg"
            )
            .split(";")[0]
            .strip()
        )

        if not mime_type.startswith(
            "image/"
        ):

            mime_type = "image/jpeg"

        return {

            "data":
                response.content,

            "mime_type":
                mime_type
        }

    except Exception as error:

        print(
            "IMAGE DOWNLOAD ERROR:",
            error
        )

        return None


# ==================================================
# GEMINI PROMPT EXTRACTION
# ==================================================

def parse_gemini_json(
    text
):

    if not text:

        return []

    text = text.strip()

    if text.startswith(
        "```"
    ):

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE
        )

        text = re.sub(
            r"\s*```$",
            "",
            text
        )

    try:

        data = json.loads(
            text
        )

    except Exception as error:

        print(
            "Gemini JSON error:",
            error
        )

        print(
            "Gemini raw response:",
            text
        )

        return []

    if isinstance(
        data,
        dict
    ):

        prompts = data.get(
            "prompts",
            []
        )

    elif isinstance(
        data,
        list
    ):

        prompts = data

    else:

        return []

    final_prompts = []

    seen = set()

    for item in prompts:

        if isinstance(
            item,
            str
        ):

            prompt_text = (
                item.strip()
            )

            source_index = None

        elif isinstance(
            item,
            dict
        ):

            prompt_text = str(
                item.get(
                    "text",
                    ""
                )
            ).strip()

            source_index = item.get(
                "source_index"
            )

        else:

            continue

        if not prompt_text:

            continue

        normalized = re.sub(
            r"\s+",
            " ",
            prompt_text.lower()
        )

        if normalized in seen:

            continue

        seen.add(
            normalized
        )

        final_prompts.append(

            {

                "source_index":
                    source_index,

                "text":
                    prompt_text
            }

        )

    return final_prompts


def extract_prompts_with_gemini(
    carousel_items
):

    if GEMINI_CLIENT is None:

        print(
            "Gemini client unavailable."
        )

        return []

    contents = []

    instructions = """
You are extracting AI generation prompts from Instagram post images.

Your job:
- Examine every supplied image carefully.
- Extract ONLY complete AI image/video generation prompts that are visibly written in the images.
- A prompt is substantial instructional text intended to be copied into an AI generator.
- Ignore cover images, decorative text, numbers, UI text, usernames, watermarks, titles, captions and calls to action.
- Ignore text such as:
  PROMPT
  AI
  @imran.prompts
  Comment AI
  Comment "AI"
  I'll send you the prompts
  Copy prompt
- Do NOT invent missing words.
- Do NOT improve the prompt.
- Do NOT summarize.
- Do NOT translate.
- Preserve the original wording, punctuation and order as accurately as possible.
- If an image does not contain a real AI prompt, ignore that image.
- Do not treat tiny fragments or random OCR-like text as prompts.
- Remove duplicate prompts.

Return ONLY valid JSON in exactly this structure:

{
  "prompts": [
    {
      "source_index": 1,
      "text": "complete prompt here"
    }
  ]
}

If there are no real prompts:

{
  "prompts": []
}
"""

    contents.append(
        instructions
    )

    image_count = 0

    for index, item in enumerate(
        carousel_items,
        start=1
    ):

        media_type = item.get(
            "media_type",
            ""
        )

        media_url = item.get(
            "media_url"
        )

        if (
            media_type == "VIDEO"
            or
            not media_url
        ):

            continue

        image = download_image_bytes(
            media_url
        )

        if not image:

            continue

        image_count += 1

        contents.append(
            f"IMAGE {index}:"
        )

        contents.append(

            types.Part.from_bytes(

                data=image[
                    "data"
                ],

                mime_type=image[
                    "mime_type"
                ]
            )

        )

    if image_count == 0:

        print(
            "No images available for Gemini."
        )

        return []

    print(
        f"Sending {image_count} "
        f"image(s) to Gemini..."
    )

    try:

        response = (
            GEMINI_CLIENT
            .models
            .generate_content(

                model=GEMINI_MODEL,

                contents=contents,

                config=
                    types.GenerateContentConfig(

                        response_mime_type=
                            "application/json",

                        temperature=0
                    )
            )
        )

        prompts = parse_gemini_json(
            response.text
        )

        print(
            "Gemini prompts found:",
            len(prompts)
        )

        return prompts

    except Exception as error:

        print(
            "GEMINI ERROR:",
            error
        )

        return []


# ==================================================
# GET OR CREATE POST
# ==================================================

def get_or_create_post(
    media_id
):

    media_id = str(
        media_id
    )

    saved = get_saved_post(
        media_id
    )

    if (
        saved
        and
        saved.get(
            "prompts"
        )
    ):

        print(
            "Using saved prompts."
        )

        return saved

    media_lock = get_media_lock(
        media_id
    )

    with media_lock:

        # Check again after waiting
        saved = get_saved_post(
            media_id
        )

        if (
            saved
            and
            saved.get(
                "prompts"
            )
        ):

            return saved

        media_info = get_instagram_media(
            media_id
        )

        if not media_info:

            return None

        carousel_items = (
            get_carousel_items(
                media_info
            )
        )

        print(
            "Carousel items:",
            len(carousel_items)
        )

        prompts = (
            extract_prompts_with_gemini(
                carousel_items
            )
        )

        if not prompts:

            print(
                "No prompts detected."
            )

            return None

        permalink = media_info.get(
            "permalink",
            ""
        )

        cover_url = media_info.get(
            "media_url",
            ""
        )

        if not cover_url and carousel_items:

            cover_url = carousel_items[0].get(
                "media_url",
                ""
            )

        save_post(
            media_id,
            permalink,
            prompts,
            cover_url
        )

        print(
            "Post prompts saved."
        )

        return get_saved_post(
            media_id
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
        cover_html = f'''<div class="post-preview" id="post-preview"><img src="{html.escape(cover_url, quote=True)}" alt="Instagram post preview" loading="eager" onerror="document.getElementById('post-preview').style.display='none'"></div>'''
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
<title>IMRAN PROMPTS</title>

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
        <div class="brand-name">IMRAN PROMPTS</div>
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
        Generated automatically for @imran.prompts
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
# PENDING ACCESS
# ==================================================

def save_pending_access(user_id, page_url):

    if not user_id or not page_url:
        return False

    with STATE_LOCK:

        data = load_json_file(
            PENDING_ACCESS_FILE,
            {}
        )

        if not isinstance(data, dict):
            data = {}

        data[str(user_id)] = {
            "page_url": page_url,
            "created_at": datetime.now(
                timezone.utc
            ).isoformat()
        }

        return save_json_file(
            PENDING_ACCESS_FILE,
            data
        )


def get_pending_access(user_id):

    if not user_id:
        return None

    with STATE_LOCK:

        data = load_json_file(
            PENDING_ACCESS_FILE,
            {}
        )

        if not isinstance(data, dict):
            return None

        return data.get(
            str(user_id)
        )


def clear_pending_access(user_id):

    if not user_id:
        return

    with STATE_LOCK:

        data = load_json_file(
            PENDING_ACCESS_FILE,
            {}
        )

        if not isinstance(data, dict):
            return

        data.pop(
            str(user_id),
            None
        )

        save_json_file(
            PENDING_ACCESS_FILE,
            data
        )


# ==================================================
# INSTAGRAM PUBLIC COMMENT REPLY
# ==================================================

def send_public_comment_reply(
    comment_id,
    username=""
):

    if not comment_id:
        return False

    try:
        reply_index = int(
            str(comment_id)[-6:]
        ) % len(PUBLIC_REPLY_MESSAGES)
    except Exception:
        reply_index = 0

    message = PUBLIC_REPLY_MESSAGES[
        reply_index
    ]

    return send_comment_reply_text(
        comment_id,
        message
    )


# ==================================================
# INSTAGRAM PRIVATE REPLY
# ==================================================

def send_access_request(
    comment_id
):

    """
    First private reply after an Instagram comment.

    We first try to include a Quick Reply button directly in
    the private reply. Some Instagram clients support this flow.
    If Meta rejects the quick-reply payload for a comment-based
    private reply, we automatically fall back to a normal text
    private reply so the automation keeps working.
    """

    if not INSTAGRAM_ACCESS_TOKEN:
        return None

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{INSTAGRAM_USER_ID}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {INSTAGRAM_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    # First attempt: private reply + Quick Reply button.
    # This gives the mobile experience closest to:
    # [ Send me the access ]
    quick_reply_payload = {
        "recipient": {
            "comment_id": comment_id
        },
        "message": {
            "text": (
                "Hey! 👋\n\n"
                "Your AI prompts are ready ✨\n\n"
                "Tap below and I’ll send you the access."
            ),
            "quick_replies": [
                {
                    "content_type": "text",
                    "title": "Send me the access",
                    "payload": "VIEW_AI_PROMPTS"
                }
            ]
        }
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=quick_reply_payload,
            timeout=30
        )

        print(
            "Private Reply + Quick Reply:",
            response.status_code
        )

        if response.ok:

            data = response.json()

            return data.get(
                "recipient_id"
            )

        # If Meta does not allow quick replies in a comment
        # private reply, keep the bot working with a text fallback.
        print(
            "Quick Reply private reply was rejected. "
            "Using text fallback."
        )
        print(response.text)

        fallback_payload = {
            "recipient": {
                "comment_id": comment_id
            },
            "message": {
                "text": (
                    "Hey! 👋\n\n"
                    "Your AI prompts are ready ✨\n\n"
                    "Reply ACCESS and I’ll send you "
                    "a button to open them."
                )
            }
        }

        response = requests.post(
            url,
            headers=headers,
            json=fallback_payload,
            timeout=30
        )

        print(
            "Private Reply fallback:",
            response.status_code
        )

        if not response.ok:
            print(response.text)
            return None

        data = response.json()

        return data.get(
            "recipient_id"
        )

    except requests.RequestException as error:

        print(
            "PRIVATE REPLY ERROR:",
            error
        )

        return None


def send_view_prompts_button(
    recipient_id,
    page_url
):

    """Send a documented Instagram Button Template after user response."""

    if (
        not INSTAGRAM_ACCESS_TOKEN
        or not recipient_id
        or not page_url
    ):
        return False

    url = (
        f"https://graph.instagram.com/"
        f"{API_VERSION}/"
        f"{INSTAGRAM_USER_ID}/messages"
    )

    headers = {
        "Authorization":
            f"Bearer {INSTAGRAM_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    payload = {
        "recipient": {
            "id": str(recipient_id)
        },
        "messaging_type": "RESPONSE",
        "message": {
            "attachment": {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": (
                        "Thanks! ✨ Your AI prompts are ready "
                        "to view and copy.\n\n"
                        "If the button is not visible on desktop, "
                        "open this link:\n"
                        f"{page_url}"
                    ),
                    "buttons": [
                        {
                            "type": "web_url",
                            "url": page_url,
                            "title": "View AI Prompts"
                        }
                    ]
                }
            }
        }
    }

    try:

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        print(
            "Button Template:",
            response.status_code
        )

        if not response.ok:
            print(response.text)

        return response.ok

    except requests.RequestException as error:

        print(
            "BUTTON TEMPLATE ERROR:",
            error
        )

        return False


# ==================================================
# COMMENT PROCESSING
# ==================================================

def process_comment(
    value,
    public_base_url
):

    comment_id = value.get(
        "id"
    )

    comment_text = value.get(
        "text",
        ""
    )

    commenter = value.get(
        "from",
        {}
    )

    username = commenter.get(
        "username",
        ""
    )

    commenter_id = commenter.get(
        "id"
    )

    media = value.get(
        "media",
        {}
    )

    media_id = media.get(
        "id"
    )

    # Ignore comments/replies created by our own Instagram account.
    # This prevents the bot's public reply (which can contain words like
    # "prompts" or "access") from triggering the automation again.
    if (
        str(commenter_id or "") == str(INSTAGRAM_USER_ID)
        or str(username or "").strip().lower() == "imran.prompts"
    ):

        print(
            "Own Instagram comment ignored:",
            comment_id
        )

        return

    if (
        not comment_id
        or
        not comment_text
    ):

        return

    words = re.findall(
        r"\b\w+\b",
        comment_text.lower()
    )

    is_prompt_request = any(
        word in TRIGGER_WORDS
        for word in words
    )

    if not is_prompt_request:

        process_smart_comment(
            comment_id,
            comment_text,
            commenter_id,
            username
        )

        return

    print(
        f"Prompt request comment from @{username}"
    )

    if not claim_comment(
        comment_id
    ):

        return

    try:

        if not media_id:

            release_comment(
                comment_id
            )

            return

        if is_request_on_cooldown(
            media_id,
            commenter_id,
            username
        ):

            print(
                "Repeated request ignored during cooldown:",
                username or commenter_id,
                media_id
            )

            mark_comment_done(
                comment_id
            )

            return

        post = get_or_create_post(
            media_id
        )

        if not post:

            print(
                "Could not create prompt page."
            )

            release_comment(
                comment_id
            )

            return

        page_url = (
            f"{public_base_url}"
            f"/p/{media_id}"
        )

        public_reply_ok = send_public_comment_reply(
            comment_id,
            username
        )

        if public_reply_ok:
            print(
                "Public reply sent successfully."
            )
        else:
            print(
                "Public reply failed; continuing with DM flow."
            )

        recipient_id = send_access_request(
            comment_id
        )

        # Meta normally returns recipient_id in the
        # private-reply response. The comment webhook also
        # contains the commenter's Instagram-scoped ID, so
        # use it as a fallback.
        recipient_id = (
            recipient_id
            or commenter_id
        )

        if recipient_id:

            save_pending_access(
                recipient_id,
                page_url
            )

            log_prompt_request(
                media_id,
                comment_id,
                commenter_id,
                username,
                page_url
            )

            mark_comment_done(
                comment_id
            )

            print(
                "Access request sent successfully."
            )

        else:

            release_comment(
                comment_id
            )

    except Exception as error:

        print(
            "COMMENT PROCESS ERROR:",
            error
        )

        release_comment(
            comment_id
        )


# ==================================================
# INCOMING INSTAGRAM MESSAGES
# ==================================================

def process_message_event(event):

    sender = event.get(
        "sender",
        {}
    )

    sender_id = sender.get(
        "id"
    )

    # Instagram can send several messaging webhook event types (for example
    # delivery/read updates) that contain a sender but no actual inbound
    # user message. Those events must never cancel a queued follow-up.
    message = event.get("message")
    if not isinstance(message, dict):
        return

    # Ignore messages generated by our own account.
    if message.get("is_echo"):
        return

    if not sender_id:
        return

    text = str(
        message.get(
            "text",
            ""
        )
    ).strip()

    quick_reply = message.get(
        "quick_reply",
        {}
    )
    if not isinstance(quick_reply, dict):
        quick_reply = {}

    # A message envelope with no text, quick reply, or attachment can also be
    # a platform/service event rather than something the person intentionally
    # sent. Ignore it so it cannot cancel a valid pending follow-up.
    attachments = message.get("attachments")
    has_user_content = bool(text or quick_reply or attachments)
    if not has_user_content:
        return

    quick_payload = str(
        quick_reply.get(
            "payload",
            ""
        )
    ).strip().upper()

    words = re.findall(
        r"\b\w+\b",
        text.lower()
    )

    wants_access = (
        "access" in words
        or "send" in words
        or quick_payload == "VIEW_AI_PROMPTS"
    )

    # IMPORTANT:
    # The access-confirmation message itself is part of the prompt delivery
    # flow, so it must NOT cancel the follow-up we are about to schedule.
    # Only a normal inbound message after the delivery flow cancels a queued
    # follow-up because the person has already re-engaged naturally.
    if not wants_access:
        cancel_pending_followups(
            sender_id,
            "user_replied"
        )
        return

    pending = get_pending_access(
        sender_id
    )

    if not pending:

        print(
            "No pending prompt page for user:",
            sender_id
        )

        return

    page_url = pending.get(
        "page_url"
    )

    if not page_url:
        return

    success = send_view_prompts_button(
        sender_id,
        page_url
    )

    if success:

        clear_pending_access(
            sender_id
        )

        schedule_prompt_followup(
            sender_id,
            page_url
        )

        print(
            "View Prompts button sent successfully."
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


def _oauth_words(value):
    return re.findall(r"\b\w+\b", str(value or "").lower())


def generate_oauth_smart_reply(comment_text):
    """Generate a tenant-neutral reply; no legacy account identity is supplied."""
    source_text = str(comment_text or "").strip()
    if not source_text or GEMINI_CLIENT is None:
        return ""
    instructions = f"""
Write one short, friendly reply to an Instagram comment for a professional account.
Use the comment's language where practical. Do not mention automation, account
names, credentials, or private information. Do not use links or hashtags.
Return only the reply text, under {SMART_REPLY_MAX_LENGTH} characters.

Comment: {source_text}
"""
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=instructions,
            config=types.GenerateContentConfig(temperature=0.7),
        )
        reply = re.sub(r"\s+", " ", str(response.text or "")).strip().strip('"\'')
        return reply[:SMART_REPLY_MAX_LENGTH].rstrip()
    except Exception:
        print("OAUTH ENGAGEMENT: smart reply unavailable.")
        return ""


def extract_oauth_prompts(media):
    """Extract only visible prompts with a generic, account-neutral Gemini prompt."""
    if GEMINI_CLIENT is None or not isinstance(media, dict):
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
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
        )
        return parse_gemini_json(str(response.text or ""))
    except Exception:
        print("OAUTH ENGAGEMENT: prompt extraction unavailable.")
        return []


def _oauth_complete(session_factory, account_id, provider_event_id, event_type, *, failed=False):
    with session_factory.begin() as db_session:
        complete_oauth_event(db_session, account_id, provider_event_id, event_type, failed=failed)


def _oauth_prompt_post_for_comment(session_factory, account_id, external_id, media_id):
    with session_factory() as db_session:
        cached = get_oauth_prompt_post(db_session, account_id, media_id)
        if cached is not None:
            return cached.public_id
    media = lookup_oauth_media(
        session_factory,
        instagram_account_id=account_id,
        instagram_user_id=external_id,
        media_id=media_id,
    )
    prompts = extract_oauth_prompts(media)
    if not prompts:
        return None
    with session_factory.begin() as db_session:
        record = oauth_prompt_post(
            db_session,
            account_id,
            media_id,
            prompts,
            permalink=str(media.get("permalink") or ""),
            cover_url=str(media.get("media_url") or media.get("thumbnail_url") or ""),
        )
        return record.public_id


def _process_oauth_comment(value, route, entry_external_id, public_base_url, session_factory):
    comment_id = str(value.get("id") or "").strip()
    comment_text = str(value.get("text") or "").strip()
    commenter = value.get("from") if isinstance(value.get("from"), dict) else {}
    commenter_id = str(commenter.get("id") or "").strip()
    username = str(commenter.get("username") or "").strip()
    media = value.get("media") if isinstance(value.get("media"), dict) else {}
    media_id = str(media.get("id") or "").strip()
    if not comment_id or not comment_text or not commenter_id or commenter_id == entry_external_id:
        return
    with session_factory() as db_session:
        automation_config = oauth_automation_runtime_config(
            db_session,
            instagram_account_id=route.instagram_account_id,
        )
    with session_factory.begin() as db_session:
        if not claim_oauth_event(db_session, route.instagram_account_id, comment_id, "comment"):
            return
    if automation_config is None:
        is_prompt_request = False
        prompt_request_decision = "runtime_config_unavailable"
    else:
        is_prompt_request = any(
            word in automation_config.trigger_keywords
            for word in _oauth_words(comment_text)
        )
        prompt_request_decision = (
            "prompt_request_matched"
            if is_prompt_request
            else "keyword_no_match"
        )
    print(f"OAUTH AUTOMATION: prompt_request_decision={prompt_request_decision}")
    try:
        if not is_prompt_request:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            with session_factory() as db_session:
                allowed = oauth_smart_reply_allowed(db_session, route.instagram_account_id, cutoff=cutoff, limit=SMART_REPLY_MAX_PER_HOUR)
            if not allowed:
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            reply = generate_oauth_smart_reply(comment_text)
            if not reply:
                _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
                return
            if not send_oauth_public_comment_reply(session_factory, instagram_account_id=route.instagram_account_id, instagram_user_id=entry_external_id, comment_id=comment_id, message=reply):
                raise RuntimeError("oauth comment delivery unavailable")
            with session_factory.begin() as db_session:
                record_oauth_smart_reply(db_session, route.instagram_account_id, comment_id, commenter_id, username, reply)
                complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
            return

        if not media_id:
            raise RuntimeError("oauth prompt media unavailable")
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=REQUEST_COOLDOWN_MINUTES)
        with session_factory() as db_session:
            cooling_down = oauth_request_on_cooldown(db_session, route.instagram_account_id, media_id, commenter_id, cutoff=cutoff)
        if cooling_down:
            _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment")
            return
        public_id = _oauth_prompt_post_for_comment(session_factory, route.instagram_account_id, entry_external_id, media_id)
        if not public_id:
            raise RuntimeError("oauth prompt extraction unavailable")
        page_url = f"{public_base_url.rstrip('/')}/p/o/{public_id}"
        if automation_config.public_reply_enabled:
            if not send_oauth_public_comment_reply(session_factory, instagram_account_id=route.instagram_account_id, instagram_user_id=entry_external_id, comment_id=comment_id, message=automation_config.public_reply_text):
                raise RuntimeError("oauth public reply unavailable")
        recipient_id = send_oauth_private_prompt_button(
            session_factory,
            instagram_account_id=route.instagram_account_id,
            instagram_user_id=entry_external_id,
            comment_id=comment_id,
            page_url=page_url,
        )
        if not recipient_id:
            raise RuntimeError("oauth private reply unavailable")
        with session_factory.begin() as db_session:
            record_oauth_request(db_session, route.instagram_account_id, media_id, comment_id, recipient_id, username, page_url)
            schedule_oauth_followup(
                db_session,
                instagram_account_id=route.instagram_account_id,
                source_message_id=comment_id,
                recipient_id=recipient_id,
                page_url=page_url,
            )
            complete_oauth_event(db_session, route.instagram_account_id, comment_id, "comment")
    except Exception:
        _oauth_complete(session_factory, route.instagram_account_id, comment_id, "comment", failed=True)
        print("OAUTH ENGAGEMENT: comment processing unavailable.")


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
    quick = message.get("quick_reply") if isinstance(message.get("quick_reply"), dict) else {}
    words = _oauth_words(message.get("text"))
    wants_access = "access" in words or "send" in words or str(quick.get("payload") or "").strip().upper() == "VIEW_AI_PROMPTS"
    if not wants_access:
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
        with session_factory.begin() as db_session:
            pop_oauth_pending_access(db_session, route.instagram_account_id, sender_id)
            schedule_oauth_followup(
                db_session,
                instagram_account_id=route.instagram_account_id,
                source_message_id=message_id,
                recipient_id=sender_id,
                page_url=page_url,
            )
            complete_oauth_event(db_session, route.instagram_account_id, message_id, "message")
    except Exception:
        _oauth_complete(session_factory, route.instagram_account_id, message_id, "message", failed=True)
        print("OAUTH ENGAGEMENT: message processing unavailable.")


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
    except Exception:
        print("OAUTH FOLLOW-UP: job claim unavailable.")
        return

    for job in claimed_jobs:
        try:
            with session_factory() as db_session:
                external_id = oauth_followup_account_external_id(
                    db_session, job.instagram_account_id
                )
            outcome = send_oauth_delayed_followup(
                session_factory,
                instagram_account_id=job.instagram_account_id,
                instagram_user_id=external_id,
                recipient_id=job.recipient_id,
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
                elif outcome == "capability_unavailable":
                    finalize_oauth_followup_failure(
                        db_session, job.id, job.lease_token,
                        category="capability_unavailable", retryable=False, now=current,
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
        try:
            route = _resolve_webhook_entry_route(entry_external_id)
        except Exception:
            print("WEBHOOK ROUTING: entry skipped (route unavailable).")
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
# DASHBOARD - GEMINI CONTENT GENERATOR
# ============================================================

@app.route("/api/generate-content", methods=["POST"])
def generate_dashboard_content():

    if GEMINI_CLIENT is None:
        return {
            "error": "Gemini client is not available."
        }, 500

    images = request.files.getlist("images")

    if not images:
        return {
            "error": "Please upload at least one image."
        }, 400

    prompt = """
You are the content assistant for the Instagram account @imran.prompts.

Analyze the uploaded image or carousel.

Generate Instagram content and return ONLY valid JSON.

Use exactly this structure:

{
  "caption": "...",
  "cta": "...",
  "hashtags": "...",
  "music_suggestions": [
    {"track": "...", "artist": "...", "reason": "..."},
    {"track": "...", "artist": "...", "reason": "..."},
    {"track": "...", "artist": "...", "reason": "..."},
    {"track": "...", "artist": "...", "reason": "..."},
    {"track": "...", "artist": "...", "reason": "..."}
  ]
}

CAPTION RULES:
- English.
- Modern, premium and natural.
- Suitable for AI photography / AI prompts content.
- Describe the visual concept.
- Do not invent personal information.
- Keep it concise.
- Do not put hashtags inside the caption.

CTA RULES:
- Short.
- Encourage the user to comment AI.
- Example style:
  "Comment AI and I'll send you the prompt."

HASHTAGS:
- Exactly 5 relevant hashtags.
- One line.
- Avoid spammy or unrelated hashtags.

MUSIC SUGGESTIONS:
- Return exactly 5 real, recognizable songs/tracks that fit the visual mood.
- Prefer recent/popular/trend-friendly music when you have confidence, but NEVER claim
  that a track is currently trending because this request has no live Instagram Music data.
- Mix strong mainstream choices with visually suitable choices.
- Give the real track title and artist.
- reason must be one short sentence explaining why it fits this specific post.
- The user will manually search these tracks inside Instagram Music before publishing.
"""

    contents = [prompt]

    image_count = 0

    for image in images[:10]:

        image_bytes = image.read()

        if not image_bytes:
            continue

        mime_type = image.mimetype or "image/jpeg"

        contents.append(
            types.Part.from_bytes(
                data=image_bytes,
                mime_type=mime_type
            )
        )

        image_count += 1

    if image_count == 0:
        return {
            "error": "The uploaded images are empty."
        }, 400

    try:

        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.7
            )
        )

        result = json.loads(response.text)

        return {
            "caption": str(
                result.get("caption", "")
            ).strip(),

            "cta": str(
                result.get("cta", "")
            ).strip(),

            "hashtags": str(
                result.get("hashtags", "")
            ).strip(),

            "music_suggestions": [
                {
                    "track": str(item.get("track", "") or "").strip(),
                    "artist": str(item.get("artist", "") or "").strip(),
                    "reason": str(item.get("reason", "") or "").strip()
                }
                for item in (result.get("music_suggestions", []) or [])[:5]
                if isinstance(item, dict) and str(item.get("track", "") or "").strip()
            ]
        }, 200

    except Exception as error:

        print(
            "DASHBOARD GEMINI ERROR:",
            error
        )

        return {
            "error": "Gemini could not generate the content."
        }, 500
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
        print("APPROVE DRAFT ERROR:", error)
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
        print("INSTAGRAM PUBLISHED POSTS ERROR:", error)
        return {"error": "Could not connect to Instagram."}, 502
    except Exception as error:
        print("INSTAGRAM PUBLISHED POSTS ERROR:", error)
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

    if GEMINI_CLIENT is None or not advanced.get("posts_analyzed"):
        return fallback

    prompt = f"""
You are analyzing Instagram performance for @imran.prompts.
Write exactly 3 concise sentences in English:
1) what is working,
2) what is weaker or changing,
3) what to focus on next week.
Do not invent numbers or claim causation. Be practical and premium, not hype.

Advanced analytics JSON:
{json.dumps(advanced, ensure_ascii=False)}
"""
    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.35),
        )
        summary = re.sub(r"\s+", " ", str(response.text or "")).strip()
        return summary[:900] if summary else fallback
    except Exception as error:
        print("ADVANCED ANALYTICS GEMINI ERROR:", error)
        return fallback


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
                        print("INSTAGRAM ANALYTICS MEDIA ERROR:", error)
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

        return {
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
        }, 200

    except requests.RequestException as error:
        print("INSTAGRAM ANALYTICS CONNECTION ERROR:", error)
        return {"error": "Could not connect to Instagram analytics."}, 502
    except Exception as error:
        print("INSTAGRAM ANALYTICS ERROR:", error)
        return {"error": "Could not load Instagram analytics."}, 500




# ============================================================
# V0.2 - SMART CONTENT PLANNER
# ============================================================

@app.route("/api/content-planner", methods=["POST"])
def generate_content_planner():
    """
    Analyze up to the latest 30 Instagram posts supplied by the dashboard's
    live analytics response and return one actionable next-post recommendation.
    This endpoint is read-only: it never publishes or schedules anything.
    """
    if GEMINI_CLIENT is None:
        return {"error": "Gemini client is not available."}, 500

    payload = request.get_json(silent=True) or {}
    raw_posts = payload.get("posts", [])
    if not isinstance(raw_posts, list) or not raw_posts:
        return {"error": "No Instagram analytics are available to analyze."}, 400

    posts = []
    for raw in raw_posts[:30]:
        if not isinstance(raw, dict):
            continue
        posts.append({
            "caption": str(raw.get("caption", "") or "")[:900],
            "media_type": str(raw.get("media_type", "") or ""),
            "timestamp": str(raw.get("timestamp", "") or ""),
            "likes": int(raw.get("like_count", 0) or 0),
            "comments": int(raw.get("comments_count", 0) or 0),
            "reach": int(raw.get("reach", 0) or 0),
            "saved": int(raw.get("saved", 0) or 0),
            "shares": int(raw.get("shares", 0) or 0),
            "views": int(raw.get("views", 0) or 0),
        })

    if not posts:
        return {"error": "No valid Instagram posts are available to analyze."}, 400

    prompt = """
You are the Smart Content Planner for @imran.prompts, an Instagram account
focused on premium AI imagery and AI prompts.

Analyze the supplied recent Instagram post performance and recommend ONE
specific next post. Use the metrics as evidence; do not invent metrics.

Return ONLY valid JSON with exactly these keys:
{
  "content_type": "...",
  "reason": "...",
  "best_time": "HH:MM",
  "caption": "...",
  "cta": "...",
  "hashtags": "..."
}

Rules:
- English output.
- content_type: short concrete creative direction, not a generic word.
- reason: maximum 2 concise sentences and mention the performance pattern.
- best_time: Morocco local time in 24-hour HH:MM format. Infer from timestamps
  of stronger posts when possible; otherwise use a sensible evening time.
- caption: premium, natural, concise, suitable for the recommended next post.
- CTA: short and encourage "Comment AI" naturally.
- hashtags: exactly 5 relevant hashtags on one line.
- Never claim certainty about causation.
- Do not publish or schedule anything.

Recent post data:
""" + json.dumps(posts, ensure_ascii=False)

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.45
            )
        )
        result = json.loads(response.text or "{}")
        required = ("content_type", "reason", "best_time", "caption", "cta", "hashtags")
        cleaned = {key: str(result.get(key, "") or "").strip() for key in required}

        if not cleaned["content_type"] or not cleaned["caption"]:
            return {"error": "Gemini returned an incomplete recommendation."}, 502

        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cleaned["best_time"]):
            cleaned["best_time"] = "20:30"

        cleaned["posts_analyzed"] = len(posts)
        cleaned["success"] = True
        return cleaned, 200

    except Exception as error:
        print("CONTENT PLANNER GEMINI ERROR:", error)
        return {"error": "Gemini could not generate the content plan."}, 500


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
        print("INSTAGRAM PERMALINK ERROR:", error)
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
            error
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
        print("CLOUDINARY REQUEST ERROR:", error)
        return None, "Cloudinary upload request failed: " + str(error)

    except Exception as error:
        print("CLOUDINARY UPLOAD ERROR:", error)
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


def generate_daily_story_comment(caption=""):
    fallback = auto_story_fallback_comment(caption)
    if GEMINI_CLIENT is None:
        return fallback

    prompt = f"""
You write one very short Instagram Story overlay for @imran.prompts.
The Story is resurfacing an older Instagram post.

Rules:
- English.
- One short line only.
- Maximum 55 characters.
- Natural and premium, not spammy.
- If the post is about AI prompts, a light 'Comment AI' CTA is allowed.
- No hashtags.
- No links.
- No quotation marks.
- Do not say the post is new.
- Return only the overlay text.

Post caption:
{str(caption or '')[:1200]}
"""

    try:
        response = GEMINI_CLIENT.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.5),
        )
        result = re.sub(r"\s+", " ", str(response.text or "")).strip().strip('"\'')
        if result:
            return result[:80]
    except Exception as error:
        print("DAILY AUTO STORY GEMINI ERROR:", error)

    return fallback


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
        print("DAILY AUTO STORY MEDIA LOOKUP ERROR:", error)
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

    # For Reels/videos, use a thumbnail so Gemini text can be overlaid safely.
    return str(post.get("thumbnail_url", "") or "").strip()


def _download_daily_archive_image(url):
    if not url:
        return None
    try:
        response = requests.get(str(url), timeout=60)
        if response.ok and response.content:
            return response.content
    except Exception as error:
        print("DAILY AUTO STORY DOWNLOAD ERROR:", error)
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
        print("SCHEDULED STORY ERROR:", error)

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
        print("CONTAINER STATUS ERROR:", error)
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
        print("SCHEDULE LOCK ERROR:", error)
        return None


def release_schedule_lock(lock_path):
    if not lock_path:
        return
    try:
        Path(lock_path).unlink(missing_ok=True)
    except Exception as error:
        print("SCHEDULE LOCK RELEASE ERROR:", error)


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
        print("SCHEDULED PUBLISH ERROR:", error)
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


def dashboard_scheduler_loop():
    print("Dashboard scheduler: ON", f"({SCHEDULER_POLL_SECONDS}s, Africa/Casablanca)")
    while True:
        try:
            # V0.2: Posts are manual-publish only. Keep Story automation active.
            process_daily_auto_story()
            process_due_scheduled_stories()
            process_due_followups()
            process_due_oauth_webhook_work()
            process_due_oauth_followups()
        except Exception as error:
            print("DASHBOARD SCHEDULER ERROR:", error)
        time.sleep(SCHEDULER_POLL_SECONDS)


def start_dashboard_scheduler():
    thread = threading.Thread(
        target=dashboard_scheduler_loop,
        name="dashboard-scheduler",
        daemon=True
    )
    thread.start()


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

    # Core AI/content dependencies.
    add_check(
        "gemini",
        "Gemini content engine",
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
    """
    Main Social Media Bot Dashboard
    """
    return render_template(
        "dashboard.html", current_user=get_current_user(), csrf_token=csrf_token()
    )


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
