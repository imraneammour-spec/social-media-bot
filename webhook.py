import os
import re
import json
import html
import threading
from pathlib import Path
from datetime import datetime, timezone

import requests

from dotenv import load_dotenv
from flask import Flask, request, send_file, Response

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

load_dotenv(ENV_FILE)

app = Flask(__name__)

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "imran_social_media_bot_2026")

INSTAGRAM_ACCESS_TOKEN = os.getenv(
    "INSTAGRAM_ACCESS_TOKEN"
)

GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY"
)

INSTAGRAM_USER_ID = "17841438617458289"

API_VERSION = "v25.0"

TRIGGER_WORDS = {"ai", "prompt", "prompts", "send", "access"}
REQUEST_COOLDOWN_MINUTES = 10
SMART_REPLY_MIN_INTERVAL_SECONDS = 15
SMART_REPLY_MAX_PER_HOUR = 20
SMART_REPLY_MAX_LENGTH = 180

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

    message = event.get(
        "message",
        {}
    )

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

    if not wants_access:
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