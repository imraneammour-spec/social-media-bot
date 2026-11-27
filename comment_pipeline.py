"""Shared comment-pipeline decisions for the legacy and OAuth handlers.

Both handlers implement the same flow:

    parse -> own-comment guard -> trigger-word check
      -> smart reply (eligible? rate-limited? generate -> send -> record)
      -> prompt request (claim -> media? -> cooldown -> prompt page
         -> public reply -> private reply -> pending access -> log -> done)

but against different state backends (legacy JSON/Postgres vs OAuth
Postgres), different tokens, and different delivery modes. Merging the
two handler bodies into one function would change behavior: the legacy
handler claims only the prompt-request path and releases the claim on
any error, while the OAuth handler claims first, gates on per-account
automation config, supports direct-message/link delivery modes, and
schedules follow-ups. So the shared part is the decision logic below,
used verbatim by both handlers, with the stage order kept identical in
each. The handlers stay separate; the decisions do not drift apart.
"""

from __future__ import annotations

import re
from typing import Any

_WORD_PATTERN = re.compile(r"\b\w+\b")

# Postenza v2 reply policy (owner rule — replies must never stop):
# there is no AI-generated "smart reply" any more. A comment that does
# not match a trigger keyword always gets this ready generic reply
# (or the account's own configured generic reply text when one exists).
# It is a plain constant on purpose: replying must never depend on an
# AI provider being configured, reachable, or returning text.
DEFAULT_GENERIC_REPLY = "شكرا على رسالتك، غادي نجاوبك قريبا"


def generic_ready_reply(configured_text=None):
    """Return the ready generic reply for a non-keyword comment.

    ``configured_text`` is an account-configured generic reply when one
    exists; otherwise the shared default above is used. Never returns "".
    """

    text = str(configured_text or "").strip()
    return text or DEFAULT_GENERIC_REPLY


def parse_comment_event(value: Any) -> dict[str, str]:
    """Normalize one Instagram comment webhook payload to plain strings.

    Missing fields become "". A non-dict ``from``/``media`` is treated as
    absent (previously this crashed the legacy handler's parse step).
    """

    if not isinstance(value, dict):
        value = {}
    commenter = value.get("from")
    if not isinstance(commenter, dict):
        commenter = {}
    media = value.get("media")
    if not isinstance(media, dict):
        media = {}
    return {
        "comment_id": str(value.get("id") or "").strip(),
        "comment_text": str(value.get("text") or "").strip(),
        "commenter_id": str(commenter.get("id") or "").strip(),
        "username": str(commenter.get("username") or "").strip(),
        "media_id": str(media.get("id") or "").strip(),
    }


def comment_words(comment_text: str) -> list[str]:
    """Lower-cased word tokens of a comment (shared trigger matching)."""

    return _WORD_PATTERN.findall(str(comment_text or "").lower())


def comment_has_trigger_word(comment_text: str, trigger_words: Any) -> bool:
    """True when any word of the comment is in the trigger set."""

    words = set(trigger_words or ())
    if not words:
        return False
    return any(word in words for word in comment_words(comment_text))


def is_own_account_comment(
    commenter_id: str,
    username: str,
    *,
    owner_id: str,
    owner_username: str | None = None,
) -> bool:
    """True when the commenter is the account itself (anti-loop guard).

    ``owner_username`` is only used by the legacy handler, which also
    matches the account by username; the OAuth handler matches by id.
    """

    if commenter_id and owner_id and str(commenter_id) == str(owner_id):
        return True
    if owner_username and str(username or "").strip().lower() == str(
        owner_username
    ).strip().lower():
        return True
    return False
