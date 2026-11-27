"""Legacy Instagram bot (extracted from webhook.py).

This module owns the original single-account ("legacy") bot for
imran.prompts: its JSON/Postgres state, prompt-request flow, smart
replies, follow-ups, and incoming-message handling. It has no Flask
routes and no dependency on webhook.py; webhook.py imports it and
re-exports the names other modules still use.

Configuration (tokens, data dir, trigger words) is read from the
environment here, exactly as webhook.py used to do it. The legacy
account keeps working exactly as before: same state, same flow, same
replies. The OAuth (multi-tenant) pipeline is separate and stays in
webhook.py.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from filelock import FileLock

import gemini_client
from comment_pipeline import (
    comment_has_trigger_word,
    is_own_account_comment,
    parse_comment_event,
)
from prompt_pages import (
    extract_prompts_with_gemini,
    get_carousel_items,
    get_instagram_media,
    get_media_lock,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)


# --- durable state: Postgres when available, JSON files as fallback ---
# The session factory is provided by webhook.py after import (it owns the
# Flask app / auth extension); until then the JSON files are used.
_SESSION_FACTORY_PROVIDER = None


def configure_legacy_state_session_factory(provider):
    """Register the callable returning the database session factory."""

    global _SESSION_FACTORY_PROVIDER
    _SESSION_FACTORY_PROVIDER = provider


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

        provider = _SESSION_FACTORY_PROVIDER
        factory = provider() if provider is not None else None
        store = legacy_state_store(factory, INSTAGRAM_USER_ID)
    except Exception:
        return None
    if store is None:
        return None
    _LEGACY_STATE_STORE = store
    return store


def _legacy_state_error(context):
    print("LEGACY STATE DB ERROR", context)


def _legacy_delivery_config():
    """Return the legacy account's saved delivery settings, or None.

    None means "no saved choice": the caller keeps today's prompt
    behavior. Any database problem also returns None, so a storage blip
    can never break the prompt flow. The row is keyed by the legacy
    account's ``InstagramAccount.id``; it is created with prompt
    defaults the first time the owner saves on /app/automations.
    """

    try:

        provider = _SESSION_FACTORY_PROVIDER

        factory = provider() if provider is not None else None

        if factory is None:

            return None

        legacy_id = str(INSTAGRAM_USER_ID or "").strip()

        if not legacy_id:

            return None

        from sqlalchemy import select

        from database.models import (
            InstagramAccount,
            InstagramAutomationConfig,
        )
        from database.tenant_automation import (
            normalize_automation_delivery_mode,
        )

        with factory() as session:

            account = session.scalar(
                select(InstagramAccount).where(
                    InstagramAccount.instagram_user_id == legacy_id
                )
            )

            if account is None:

                return None

            config = session.scalar(
                select(InstagramAutomationConfig).where(
                    InstagramAutomationConfig.instagram_account_id == account.id
                )
            )

            if config is None:

                return None

            return {
                "delivery_mode": normalize_automation_delivery_mode(
                    getattr(config, "delivery_mode", "prompt_request")
                    or "prompt_request"
                ),
                "public_reply_enabled": bool(
                    getattr(config, "public_reply_enabled", True)
                ),
                "public_reply_text": str(
                    getattr(config, "public_reply_text", "") or ""
                ),
                "private_message_text": str(
                    getattr(config, "private_message_text", "") or ""
                ),
                "destination_url": str(
                    getattr(config, "destination_url", "") or ""
                ),
            }

    except Exception:

        return None


API_VERSION = "v25.0"

RAILWAY_DATA_DIR = Path("/app/data")

DATA_DIR = (
    Path(os.environ["POSTENZA_DATA_DIR"])
    if os.environ.get("POSTENZA_DATA_DIR")
    else RAILWAY_DATA_DIR
    if RAILWAY_DATA_DIR.exists()
    else BASE_DIR / "data"
)

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

SCHEDULER_POLL_SECONDS = 15

TRIGGER_WORDS = {"ai", "prompt", "prompts", "send", "access"}

REQUEST_COOLDOWN_MINUTES = 10

SMART_REPLY_MIN_INTERVAL_SECONDS = 15

SMART_REPLY_MAX_PER_HOUR = 20

SMART_REPLY_MAX_LENGTH = 180

FOLLOWUP_DELAY_HOURS = 6

FOLLOWUP_MAX_AGE_HOURS = 23

FOLLOWUP_COOLDOWN_DAYS = 7

INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()

LEGACY_INSTAGRAM_USER_ID = os.getenv("LEGACY_INSTAGRAM_USER_ID", "").strip()

INSTAGRAM_USER_ID = LEGACY_INSTAGRAM_USER_ID

PUBLIC_REPLY_MESSAGES = [
    "Sent! Check your DMs ✨",
    "Your prompts are ready 👀 Check your DMs.",
    "Done ✨ I sent you the access in DM.",
    "Check your DMs 🚀 Your AI prompts are waiting."
]

STATE_LOCK = FileLock(str(DATA_DIR / ".state.lock"), timeout=60)

DATA_DIR.mkdir(parents=True, exist_ok=True)


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
            type(error).__name__
        )

        return default


def save_json_file(path, data):
    import tempfile
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".postenza-", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        return True
    except Exception as error:
        print(f"JSON SAVE ERROR {path.name}:", type(error).__name__)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return False


def load_processed_comments():

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.load_processed_comments()
        except Exception:
            _legacy_state_error("load_processed_comments")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            claimed = store.claim_comment(comment_id)
            if not claimed:
                print(
                    "Duplicate comment ignored:",
                    comment_id
                )
            return claimed
        except Exception:
            _legacy_state_error("claim_comment")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            store.mark_comment_done(comment_id)
            return
        except Exception:
            _legacy_state_error("mark_comment_done")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            store.release_comment(comment_id)
            return
        except Exception:
            _legacy_state_error("release_comment")

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


def load_posts():

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.load_posts()
        except Exception:
            _legacy_state_error("load_posts")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.get_saved_post(media_id)
        except Exception:
            _legacy_state_error("get_saved_post")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            store.save_post(media_id, permalink, prompts, cover_url=cover_url)
            return
        except Exception:
            _legacy_state_error("save_post")

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


def load_requests():

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.load_requests()
        except Exception:
            _legacy_state_error("load_requests")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            latest = store.latest_request_for_key(key)
        except Exception:
            _legacy_state_error("latest_request_for_key")
            latest = None
            store = None
        else:
            if latest is None:
                return False
            created_at = latest.get("created_at")
            if not created_at:
                return False
            try:
                previous = datetime.fromisoformat(created_at)
            except Exception:
                return False
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=timezone.utc)
            elapsed_seconds = (now - previous).total_seconds()
            cooldown_seconds = REQUEST_COOLDOWN_MINUTES * 60
            return elapsed_seconds < cooldown_seconds

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

    store = _legacy_state_store()
    if store is not None:
        try:
            store.log_prompt_request(entry)
            return
        except Exception:
            _legacy_state_error("log_prompt_request")

    with STATE_LOCK:

        data = load_requests()

        data.append(entry)

        if len(data) > 5000:
            data = data[-5000:]

        save_json_file(
            REQUESTS_FILE,
            data
        )


def load_followups():
    data = load_json_file(FOLLOWUPS_FILE, [])
    return data if isinstance(data, list) else []


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
                "comment AI on any post and I'll send it to you."
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

    if gemini_client.GEMINI_CLIENT is None:
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

        response = gemini_client._gemini_generate_content(instructions)

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
            _sanitized_gemini_error(error)
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
            type(error).__name__
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
            type(error).__name__
        )

        release_comment(
            comment_id
        )


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


def save_pending_access(user_id, page_url):

    if not user_id or not page_url:
        return False

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.save_pending_access(user_id, page_url)
        except Exception:
            _legacy_state_error("save_pending_access")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            return store.get_pending_access(user_id)
        except Exception:
            _legacy_state_error("get_pending_access")

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

    store = _legacy_state_store()
    if store is not None:
        try:
            store.clear_pending_access(user_id)
            return
        except Exception:
            _legacy_state_error("clear_pending_access")

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


def send_private_comment_reply_text(
    comment_id,
    text
):

    """Send a plain-text private reply to a commenter.

    Same endpoint as the prompt flow's private reply, without the quick
    reply button: used for direct_message / link delivery, where the
    configured message itself is the delivery. Returns the recipient id
    on success, None otherwise. The prompt flow's own
    ``send_access_request`` is left untouched.
    """

    if (
        not INSTAGRAM_ACCESS_TOKEN
        or not comment_id
        or not text
    ):

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

    payload = {
        "recipient": {
            "comment_id": comment_id
        },
        "message": {
            "text": text
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
            "Private Reply:",
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
            type(error).__name__
        )

        return None


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
                "Tap below and I'll send you the access."
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
                    "Reply ACCESS and I'll send you "
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
            type(error).__name__
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
            type(error).__name__
        )

        return False


def _process_legacy_direct_delivery(
    delivery,
    comment_id,
    media_id,
    commenter_id,
    username
):

    """Handle one trigger comment in direct_message / link mode.

    No prompt extraction and no prompt page: the configured private
    message goes straight to the commenter by DM (link mode appends the
    configured destination URL), with the configured public reply first
    when enabled. The send is logged like a prompt request (with no page
    URL) so the same 10-minute cooldown applies to repeat triggers.
    Called from inside ``process_comment``'s try block, so errors are
    handled there.
    """

    if delivery.get(
        "public_reply_enabled",
        True,
    ):

        reply_text = str(
            delivery.get(
                "public_reply_text",
                "",
            ) or ""
        ).strip()

        if reply_text:

            send_comment_reply_text(
                comment_id,
                reply_text,
            )

    message_text = str(
        delivery.get(
            "private_message_text",
            "",
        ) or ""
    ).strip()

    if delivery.get("delivery_mode") == "link":

        destination_url = str(
            delivery.get(
                "destination_url",
                "",
            ) or ""
        ).strip()

        if destination_url:

            message_text = (
                f"{message_text}\n\n{destination_url}"
                if message_text
                else destination_url
            )

    if not message_text:

        release_comment(
            comment_id
        )

        return

    recipient_id = send_private_comment_reply_text(
        comment_id,
        message_text,
    )

    if recipient_id:

        log_prompt_request(
            media_id,
            comment_id,
            commenter_id,
            username,
            "",
        )

        mark_comment_done(
            comment_id
        )

        print(
            "Direct message sent successfully."
        )

    else:

        release_comment(
            comment_id
        )


def process_comment(
    value,
    public_base_url
):

    # Shared pipeline decisions (see comment_pipeline.py): parse, own
    # comment guard, trigger-word check. The stage order below is the
    # same as _process_oauth_comment's.
    parsed = parse_comment_event(value)
    comment_id = parsed["comment_id"]
    comment_text = parsed["comment_text"]
    commenter_id = parsed["commenter_id"]
    username = parsed["username"]
    media_id = parsed["media_id"]

    # Ignore comments/replies created by our own Instagram account.
    # This prevents the bot's public reply (which can contain words like
    # "prompts" or "access") from triggering the automation again.
    if is_own_account_comment(
        commenter_id,
        username,
        owner_id=INSTAGRAM_USER_ID,
        owner_username="imran.prompts",
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

    is_prompt_request = comment_has_trigger_word(comment_text, TRIGGER_WORDS)

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

        delivery = _legacy_delivery_config()

        if delivery is not None and delivery.get(
            "delivery_mode"
        ) in (
            "direct_message",
            "link",
        ):

            _process_legacy_direct_delivery(
                delivery,
                comment_id,
                media_id,
                commenter_id,
                username,
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
            type(error).__name__
        )

        release_comment(
            comment_id
        )


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


def _parse_iso_datetime(value):
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
