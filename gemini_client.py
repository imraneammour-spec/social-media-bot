"""Gemini client, retry, and fallback (extracted from webhook.py).

This module owns everything about talking to Gemini: the shared client
built from ``GEMINI_API_KEY``, the primary/fallback model selection, the
transient-503 retry with backoff, and the safe error summary (API keys
redacted). It has no dependency on the Flask app or on webhook.py, so
tests can import and patch it in isolation.

BYOK: :func:`_gemini_generate_content` accepts an optional per-account
``api_key``; when given, the call uses a client built with that key
instead of the shared ``GEMINI_CLIENT``. Callers that need a user key
resolve it via :mod:`database.gemini_keys` and pass it in.
"""

from __future__ import annotations

import os
import re
import time

from google import genai
from google.genai import types

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.7-flash").strip()


def _sanitized_gemini_error(error):
    """Return a short, safe Gemini error summary for logs (no credentials)."""
    detail = str(error or "")[:300]
    detail = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[API_KEY_REDACTED]", detail)
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


def _gemini_generate_with_retry(generate, *, attempts=3, base_delay=5.0):
    """Call generate() (a zero-arg callable), retrying transient 503s.

    Gemini returns 503 UNAVAILABLE when a model is under high demand and
    says to try again later, so retry those with backoff. Other errors
    (bad key, unknown model, bad request) are permanent and raise at once.
    """
    last_error = None
    for attempt in range(attempts):
        try:
            return generate()
        except Exception as error:
            last_error = error
            detail = _sanitized_gemini_error(error)
            transient = "503" in detail or type(error).__name__ == "ServerError"
            if not transient or attempt == attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            print(f"GEMINI RETRY: attempt {attempt + 1} failed ({detail}); retrying in {delay:.0f}s")
            time.sleep(delay)
    raise last_error


def build_gemini_client(api_key):
    """Build a client for one API key (shared or BYOK)."""

    return genai.Client(api_key=api_key)


def _gemini_generate_content(contents, config=None, api_key=None):
    """Generate content, trying the primary model then the fallback model.

    Each model gets up to 3 attempts with backoff on transient 503s
    (via _gemini_generate_with_retry). Raises the last error if both fail.

    ``api_key`` is a per-account BYOK key: when given, the call uses a
    client built with that key instead of the shared ``GEMINI_CLIENT``.
    When omitted, the shared key is used (legacy account behavior).
    """
    if config is None:
        config = types.GenerateContentConfig()
    client = GEMINI_CLIENT
    if api_key:
        client = build_gemini_client(api_key)
    if client is None:
        raise RuntimeError("Gemini client is unavailable.")
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models.append(GEMINI_FALLBACK_MODEL)
    last_error = None
    for model in models:
        try:
            return _gemini_generate_with_retry(
                lambda m=model: client.models.generate_content(
                    model=m,
                    contents=contents,
                    config=config,
                )
            )
        except Exception as error:
            last_error = error
            if model != models[-1]:
                print(f"GEMINI: model {model} failed ({_sanitized_gemini_error(error)}); trying fallback {models[-1]}")
    raise last_error


if GEMINI_API_KEY:
    GEMINI_CLIENT = build_gemini_client(GEMINI_API_KEY)
    print(f"Gemini model: {GEMINI_MODEL}")
else:
    GEMINI_CLIENT = None
