"""Instagram media lookup + Gemini prompt extraction (from webhook.py).

This module owns the prompt-page data path: fetching Instagram media
(via the Graph API), downloading carousel images, and extracting the AI
generation prompts visibly written in them with Gemini. It depends only
on :mod:`gemini_client` (for generation) and the environment (for the
Instagram token), never on the Flask app or webhook.py, so the legacy
bot and the OAuth pipeline can both use it.

The prompt-page *routes* (``/p/<id>`` etc.) stay in webhook.py; the
extraction they rely on lives here.
"""

from __future__ import annotations

import json
import os
import re
import threading

import requests
from google.genai import types

import gemini_client

# Same values as webhook.py's config section; read here so this module
# stays importable without the Flask app.
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "").strip()
API_VERSION = "v25.0"

MEDIA_LOCKS_LOCK = threading.Lock()
MEDIA_LOCKS = {}


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
            type(error).__name__
        )

        return None


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
            type(error).__name__
        )

        return None


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
            type(error).__name__
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

    if gemini_client.GEMINI_CLIENT is None:

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

        response = gemini_client._gemini_generate_content(
            contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
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
            type(error).__name__
        )

        return []
