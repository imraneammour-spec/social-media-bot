"""OAuth-only Graph operations with just-in-time encrypted credential use.

These helpers deliberately have no dependency on Flask configuration or the
legacy process-global Instagram token.  The account UUID and verified external
Instagram ID come from server-side webhook routing, and credentials are
decrypted only while making the individual HTTPS request.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

import requests

from .account_runtime import RuntimeCredentialUnavailableError, resolve_oauth_instagram_account_runtime
from .oauth_capability import assess_oauth_engagement_capability


API_VERSION = "v25.0"
GRAPH_ROOT = "https://graph.instagram.com"


class OAuthGraphOperationError(ValueError):
    """Generic, credential-safe error for an OAuth Graph operation."""


def _unavailable() -> OAuthGraphOperationError:
    return OAuthGraphOperationError("Instagram engagement is temporarily unavailable.")


def _account_id(value: object) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _unavailable() from None


def oauth_engagement_capability_state(
    session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object,
) -> str:
    """Assess readiness without decrypting a credential or sending a Graph request."""

    account_id = _account_id(instagram_account_id)
    try:
        with session_factory() as session:
            return assess_oauth_engagement_capability(
                session,
                instagram_account_id=account_id,
                instagram_user_id=instagram_user_id,
            ).state
    except Exception:
        return "temporarily_unavailable"


def _require_active_oauth_engagement(
    session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object,
) -> None:
    if oauth_engagement_capability_state(
        session_factory,
        instagram_account_id=instagram_account_id,
        instagram_user_id=instagram_user_id,
    ) != "active":
        raise _unavailable()


def _request_with_runtime(
    session_factory: Callable[[], Any], *, instagram_account_id: object,
    instagram_user_id: object, method: str, path: str, json_body: dict[str, object] | None = None,
    params: dict[str, str] | None = None, timeout: int = 20,
) -> dict[str, object]:
    """Make exactly one Graph request, without retaining a plaintext token."""
    account_id = _account_id(instagram_account_id)
    if not isinstance(path, str) or not path.startswith("/") or "?" in path:
        raise _unavailable()
    try:
        _require_active_oauth_engagement(
            session_factory,
            instagram_account_id=account_id,
            instagram_user_id=instagram_user_id,
        )
        with session_factory() as session:
            runtime = resolve_oauth_instagram_account_runtime(
                session,
                instagram_account_id=account_id,
                instagram_user_id=instagram_user_id,
            )
            # The only lifetime of plaintext is this in-process HTTPS call.
            response = requests.request(
                method,
                f"{GRAPH_ROOT}/{API_VERSION}{path}",
                headers={"Authorization": f"Bearer {runtime.access_token}"},
                json=json_body,
                params=params,
                timeout=timeout,
            )
        if not response.ok:
            raise _unavailable()
        payload = response.json()
        if not isinstance(payload, dict):
            raise _unavailable()
        return payload
    except (RuntimeCredentialUnavailableError, requests.RequestException, ValueError, TypeError):
        raise _unavailable() from None


def lookup_oauth_media(session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object, media_id: object) -> dict[str, object]:
    media = str(media_id or "").strip()
    if not media:
        raise _unavailable()
    return _request_with_runtime(
        session_factory,
        instagram_account_id=instagram_account_id,
        instagram_user_id=instagram_user_id,
        method="GET",
        path=f"/{media}",
        params={"fields": "id,media_type,media_url,thumbnail_url,permalink,children{media_type,media_url,thumbnail_url}"},
    )


def send_oauth_public_comment_reply(session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object, comment_id: object, message: object) -> bool:
    comment = str(comment_id or "").strip()
    text = str(message or "").strip()
    if not comment or not text:
        return False
    try:
        _request_with_runtime(session_factory, instagram_account_id=instagram_account_id, instagram_user_id=instagram_user_id, method="POST", path=f"/{comment}/replies", json_body={"message": text})
        return True
    except OAuthGraphOperationError:
        return False


def send_oauth_private_access_reply(session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object, comment_id: object) -> str | None:
    comment = str(comment_id or "").strip()
    if not comment:
        return None
    try:
        payload = _request_with_runtime(session_factory, instagram_account_id=instagram_account_id, instagram_user_id=instagram_user_id, method="POST", path=f"/{instagram_user_id}/messages", json_body={"recipient": {"comment_id": comment}, "message": {"text": "Your requested prompts are ready. Reply ACCESS to open them.", "quick_replies": [{"content_type": "text", "title": "View prompts", "payload": "VIEW_AI_PROMPTS"}]}})
        recipient = payload.get("recipient_id")
        return str(recipient).strip() if recipient is not None and str(recipient).strip() else None
    except OAuthGraphOperationError:
        return None


def send_oauth_prompt_access_button(session_factory: Callable[[], Any], *, instagram_account_id: object, instagram_user_id: object, recipient_id: object, page_url: object) -> bool:
    recipient = str(recipient_id or "").strip()
    url = str(page_url or "").strip()
    if not recipient or not url.startswith("https://"):
        return False
    body = {"recipient": {"id": recipient}, "messaging_type": "RESPONSE", "message": {"attachment": {"type": "template", "payload": {"template_type": "button", "text": "Your requested prompts are ready to view.", "buttons": [{"type": "web_url", "url": url, "title": "View prompts"}]}}}}
    try:
        _request_with_runtime(session_factory, instagram_account_id=instagram_account_id, instagram_user_id=instagram_user_id, method="POST", path=f"/{instagram_user_id}/messages", json_body=body)
        return True
    except OAuthGraphOperationError:
        return False


def send_oauth_delayed_followup(
    session_factory: Callable[[], Any], *, instagram_account_id: object,
    instagram_user_id: object, recipient_id: object,
) -> str:
    """Send one OAuth-only follow-up with a sanitized delivery outcome.

    ``retryable_pre_dispatch`` means credential resolution failed before a
    request was attempted.  A transport exception after ``requests.post`` is
    considered ambiguous and is never safe to retry automatically.
    """
    account_id = _account_id(instagram_account_id)
    external_id = str(instagram_user_id or "").strip()
    recipient = str(recipient_id or "").strip()
    if not external_id or not recipient:
        return "retryable_pre_dispatch"
    capability = oauth_engagement_capability_state(
        session_factory,
        instagram_account_id=account_id,
        instagram_user_id=external_id,
    )
    if capability != "active":
        return "retryable_pre_dispatch" if capability == "temporarily_unavailable" else "capability_unavailable"
    try:
        with session_factory() as session:
            runtime = resolve_oauth_instagram_account_runtime(
                session,
                instagram_account_id=account_id,
                instagram_user_id=external_id,
            )
            try:
                response = requests.post(
                    f"{GRAPH_ROOT}/{API_VERSION}/{external_id}/messages",
                    headers={
                        "Authorization": f"Bearer {runtime.access_token}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "recipient": {"id": recipient},
                        "messaging_type": "RESPONSE",
                        "message": {
                            "text": "Hope the prompts helped. Comment AI anytime for another set."
                        },
                    },
                    timeout=20,
                )
            except requests.RequestException:
                return "ambiguous_graph_failure"
    except (RuntimeCredentialUnavailableError, OAuthGraphOperationError, ValueError, TypeError):
        return "retryable_pre_dispatch"
    return "success" if response.ok else "provider_rejected"
