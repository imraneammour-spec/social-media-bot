import os
import re
import json
import html
import hashlib
import threading
import uuid
import time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from dotenv import load_dotenv
from flask import Flask, request, send_file, Response, url_for
from werkzeug.utils import secure_filename

from google import genai
from google.genai import types


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
DRAFTS_FILE = DATA_DIR / "dashboard_drafts.json"
DRAFT_IMAGES_DIR = DATA_DIR / "dashboard_drafts"

load_dotenv(ENV_FILE)

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "imran_social_media_bot_2026")

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

INSTAGRAM_USER_ID = "17841438617458289"

API_VERSION = "v25.0"

TRIGGER_WORDS = {"ai", "prompt", "prompts", "send", "access"}
REQUEST_COOLDOWN_MINUTES = 10
SMART_REPLY_MIN_INTERVAL_SECONDS = 15
SMART_REPLY_MAX_PER_HOUR = 20
SMART_REPLY_MAX_LENGTH = 180
FOLLOWUP_DELAY_HOURS = 6
FOLLOWUP_MAX_AGE_HOURS = 23
FOLLOWUP_COOLDOWN_DAYS = 7

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
        and token == VERIFY_TOKEN
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

def process_webhook_background(
    data,
    public_base_url
):

    try:

        entries = data.get(
            "entry",
            []
        )

        for entry in entries:

            changes = entry.get(
                "changes",
                []
            )

            for change in changes:

                field = change.get(
                    "field"
                )

                value = change.get(
                    "value",
                    {}
                )

                if field == "comments":

                    process_comment(
                        value,
                        public_base_url
                    )

            messaging_events = entry.get(
                "messaging",
                []
            )

            for event in messaging_events:

                process_message_event(
                    event
                )

    except Exception as error:

        print(
            "BACKGROUND ERROR:",
            error
        )


# ==================================================
# RECEIVE WEBHOOK
# ==================================================

@app.route(
    "/webhook",
    methods=["POST"]
)
def receive_webhook():

    data = request.get_json(
        silent=True
    ) or {}

    public_base_url = (
        request.host_url
        .rstrip("/")
    )

    worker = threading.Thread(

        target=
            process_webhook_background,

        args=(
            data,
            public_base_url
        ),

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
  "hashtags": "..."
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
            ).strip()
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



@app.route("/api/instagram-analytics", methods=["GET"])
def get_instagram_analytics():
    """Return resilient Instagram analytics for the latest media.

    Likes/comments come from media fields. Media insights are requested
    one metric at a time so an unsupported metric for one media type does
    not break the whole Analytics page.
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

    def graph_get(path, params=None):
        query = dict(params or {})
        query["access_token"] = INSTAGRAM_ACCESS_TOKEN
        response = requests.get(
            f"https://graph.instagram.com/{API_VERSION}/{path.lstrip('/')}",
            params=query,
            timeout=30
        )
        try:
            data = response.json()
        except Exception:
            data = {}
        return response, data

    try:
        # Account-level summary. These fields are available for professional
        # Instagram accounts; if one is unavailable we still return media data.
        account = {
            "id": str(INSTAGRAM_USER_ID),
            "username": "",
            "followers_count": 0,
            "media_count": 0
        }
        account_response, account_data = graph_get(
            str(INSTAGRAM_USER_ID),
            {"fields": "id,username,followers_count,media_count"}
        )
        if account_response.ok and isinstance(account_data, dict):
            account["id"] = str(account_data.get("id", INSTAGRAM_USER_ID) or INSTAGRAM_USER_ID)
            account["username"] = str(account_data.get("username", "") or "")
            account["followers_count"] = as_int(account_data.get("followers_count"))
            account["media_count"] = as_int(account_data.get("media_count"))

        # Get the latest posts and the reliable public engagement counters.
        media_response, media_data = graph_get(
            f"{INSTAGRAM_USER_ID}/media",
            {
                "fields": (
                    "id,caption,media_type,media_url,thumbnail_url,permalink,"
                    "timestamp,like_count,comments_count"
                ),
                "limit": 12
            }
        )

        # Some account/API combinations can reject one of the counters.
        # Retry with the basic fields instead of failing the whole page.
        if not media_response.ok:
            media_response, media_data = graph_get(
                f"{INSTAGRAM_USER_ID}/media",
                {
                    "fields": (
                        "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp"
                    ),
                    "limit": 12
                }
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

        posts = []
        totals = {
            "posts": 0,
            "likes": 0,
            "comments": 0,
            "reach": 0,
            "saved": 0,
            "shares": 0,
            "views": 0,
            "impressions": 0
        }

        # Query each insight separately. Instagram exposes different metrics
        # depending on IMAGE / CAROUSEL_ALBUM / VIDEO / REELS, so unsupported
        # metrics are simply omitted rather than turning the endpoint into an error.
        insight_metrics = ("reach", "saved", "shares", "views", "impressions")

        for raw in raw_posts:
            if not isinstance(raw, dict):
                continue

            media_id = str(raw.get("id", "") or "")
            if not media_id:
                continue

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
                "available_insights": []
            }

            # If counters were absent from the media list response, try the
            # individual media node. Failure here is harmless.
            if "like_count" not in raw or "comments_count" not in raw:
                count_response, count_data = graph_get(
                    media_id,
                    {"fields": "like_count,comments_count"}
                )
                if count_response.ok and isinstance(count_data, dict):
                    post["like_count"] = as_int(count_data.get("like_count"))
                    post["comments_count"] = as_int(count_data.get("comments_count"))

            for metric in insight_metrics:
                insight_response, insight_data = graph_get(
                    f"{media_id}/insights",
                    {"metric": metric}
                )
                if not insight_response.ok or not isinstance(insight_data, dict):
                    continue

                rows = insight_data.get("data", [])
                if not isinstance(rows, list) or not rows:
                    continue

                value = 0
                first = rows[0] if isinstance(rows[0], dict) else {}
                # Current API responses can expose a direct value or a values array.
                if "value" in first:
                    value = as_int(first.get("value"))
                else:
                    values = first.get("values", [])
                    if isinstance(values, list) and values:
                        last_value = values[-1] if isinstance(values[-1], dict) else {}
                        value = as_int(last_value.get("value"))

                post[metric] = value
                post["available_insights"].append(metric)

            totals["posts"] += 1
            totals["likes"] += post["like_count"]
            totals["comments"] += post["comments_count"]
            totals["reach"] += post["reach"]
            totals["saved"] += post["saved"]
            totals["shares"] += post["shares"]
            totals["views"] += post["views"]
            totals["impressions"] += post["impressions"]

            posts.append(post)

        # Highest-engagement first for the "Top posts" area.
        ranked = sorted(
            posts,
            key=lambda item: (
                item.get("like_count", 0)
                + item.get("comments_count", 0)
                + item.get("saved", 0)
                + item.get("shares", 0)
            ),
            reverse=True
        )

        return {
            "account": account,
            "totals": totals,
            "posts": posts,
            "top_posts": ranked[:6],
            "note": (
                "Reach, saves, shares, views and impressions depend on the "
                "Instagram media type and the permissions available to this account."
            )
        }, 200

    except requests.RequestException as error:
        print("INSTAGRAM ANALYTICS CONNECTION ERROR:", error)
        return {"error": "Could not connect to Instagram analytics."}, 502
    except Exception as error:
        print("INSTAGRAM ANALYTICS ERROR:", error)
        return {"error": "Could not load Instagram analytics."}, 500


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

    payload = request.get_json(silent=True) or {}
    draft_id = str(payload.get("draft_id", "") or "").strip()

    if not draft_id:
        return {"error": "Draft ID is required."}, 400

    publish_lock_path = None

    with STATE_LOCK:
        drafts = load_dashboard_drafts()

        draft = next(
            (
                item
                for item in drafts
                if str(item.get("id", "")) == draft_id
            ),
            None
        )

        if not draft:
            return {"error": "Draft not found."}, 404

        if draft.get("status") == "published":
            return {
                "error": "This draft has already been published.",
                "instagram_media_id": draft.get("instagram_media_id")
            }, 409

        was_scheduled = draft.get("status") == "scheduled"

        # Work with a copy outside the lock while the network request runs.
        draft_copy = dict(draft)

    if was_scheduled:
        publish_lock_path = acquire_schedule_lock(draft_id)
        if not publish_lock_path:
            return {
                "error": "This scheduled post is currently being processed. Try again in a moment."
            }, 409

    # Railway sits behind a reverse proxy. Flask can see the internal
    # request as HTTP even though the public site is HTTPS. Instagram
    # requires a publicly reachable HTTPS media URL, so build the public
    # base URL explicitly from Railway/proxy information.
    railway_domain = str(
        os.getenv("RAILWAY_PUBLIC_DOMAIN", "") or ""
    ).strip()

    if railway_domain:
        public_base_url = f"https://{railway_domain}".rstrip("/")
    else:
        forwarded_host = str(
            request.headers.get("X-Forwarded-Host")
            or request.host
            or ""
        ).split(",")[0].strip()

        if not forwarded_host:
            return {
                "error": "Could not determine the public Railway domain."
            }, 500

        public_base_url = f"https://{forwarded_host}".rstrip("/")

    print("Instagram public media base URL:", public_base_url)

    instagram_media_id, error_text = publish_instagram_draft(
        draft_copy,
        public_base_url
    )

    if not instagram_media_id:
        if publish_lock_path:
            release_schedule_lock(publish_lock_path)
        return {
            "error": error_text or "Instagram publishing failed."
        }, 500

    published_at = datetime.now(timezone.utc).isoformat()

    with STATE_LOCK:
        drafts = load_dashboard_drafts()

        for item in drafts:
            if str(item.get("id", "")) == draft_id:
                item["status"] = "published"
                item["published_at"] = published_at
                item["instagram_media_id"] = instagram_media_id
                break

        save_json_file(DRAFTS_FILE, drafts)

    print(
        "Dashboard draft published:",
        draft_id,
        instagram_media_id
    )

    if publish_lock_path:
        release_schedule_lock(publish_lock_path)

    return {
        "success": True,
        "draft_id": draft_id,
        "status": "published",
        "instagram_media_id": instagram_media_id
    }, 200


# ============================================================
# DASHBOARD - SCHEDULED PUBLISHING
# ============================================================

SCHEDULER_TIMEZONE = ZoneInfo("Africa/Casablanca")
SCHEDULER_POLL_SECONDS = 15
SCHEDULER_LOCKS_DIR = DATA_DIR / "scheduler_locks"
SCHEDULER_LOCKS_DIR.mkdir(parents=True, exist_ok=True)


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
    payload = request.get_json(silent=True) or {}
    draft_id = str(payload.get("draft_id", "") or "").strip()
    scheduled_value = str(payload.get("scheduled_for", "") or "").strip()

    if not draft_id:
        return {"error": "Draft ID is required."}, 400

    scheduled_utc, schedule_error = parse_dashboard_schedule(scheduled_value)
    if not scheduled_utc:
        return {"error": schedule_error}, 400

    scheduled_local = scheduled_utc.astimezone(SCHEDULER_TIMEZONE)

    with STATE_LOCK:
        drafts = load_dashboard_drafts()
        draft = next((item for item in drafts if str(item.get("id", "")) == draft_id), None)

        if not draft:
            return {"error": "Draft not found."}, 404

        if draft.get("status") == "published":
            return {
                "error": "This draft has already been published.",
                "instagram_media_id": draft.get("instagram_media_id")
            }, 409

        draft["status"] = "scheduled"
        draft["scheduled_for"] = scheduled_utc.isoformat()
        draft["scheduled_local"] = scheduled_local.isoformat()
        draft["scheduled_timezone"] = "Africa/Casablanca"
        draft["schedule_error"] = None
        save_json_file(DRAFTS_FILE, drafts)

    print("Dashboard draft scheduled:", draft_id, scheduled_local.isoformat())
    return {
        "success": True,
        "draft_id": draft_id,
        "status": "scheduled",
        "scheduled_for": scheduled_utc.isoformat(),
        "scheduled_local": scheduled_local.isoformat(),
        "timezone": "Africa/Casablanca"
    }, 200


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
            process_due_scheduled_posts()
            process_due_scheduled_stories()
            process_due_followups()
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

from flask import render_template

# Dashboard folders
app.template_folder = "dashboard/templates"
app.static_folder = "dashboard/static"


@app.route("/dashboard", methods=["GET"])
def dashboard():
    """
    Main Social Media Bot Dashboard
    """
    return render_template("dashboard.html")


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