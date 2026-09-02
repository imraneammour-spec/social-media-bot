"""Tenant-safe execution for due scheduled Instagram image content."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .account_runtime import (
    InstagramAccountRuntime,
    RuntimeCredentialUnavailableError,
    resolve_oauth_instagram_account_runtime,
)
from .models import InstagramContentItem


LOGGER = logging.getLogger(__name__)
API_VERSION = "v25.0"
GRAPH_ROOT = "https://graph.instagram.com"
IMAGE_CONTENT_TYPE = "image"
CONTAINER_STATUS_MAX_ATTEMPTS = 6
CONTAINER_STATUS_POLL_INTERVAL_SECONDS = 2
SAFE_ERROR_CODES = {
    "unsupported_content_type",
    "instagram_connection_unavailable",
    "tenant_identity_mismatch",
    "provider_rejected",
    "provider_container_failed",
    "provider_container_not_ready",
    "provider_response_invalid",
    "provider_request_ambiguous",
    "unexpected_publish_error",
}
_SAFE_PUBLISH_STAGES = frozenset(("container_create", "container_status", "media_publish"))
_SAFE_PROVIDER_ERROR_TYPES = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_CONTAINER_STATUSES = frozenset(("ERROR", "EXPIRED", "FINISHED", "IN_PROGRESS", "PUBLISHED"))
_SAFE_PROVIDER_ERROR_CLASSIFICATIONS = frozenset((
    "provider_error_metadata",
    "provider_error_unparseable",
))


class InstagramPublishError(ValueError):
    """A safe provider outcome classification with no provider payload."""

    def __init__(
        self,
        code: str,
        *,
        request_may_have_been_sent: bool,
        safe_diagnostics: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "unexpected_publish_error"
        self.request_may_have_been_sent = bool(request_may_have_been_sent)
        self.safe_diagnostics = _safe_provider_diagnostics(safe_diagnostics)
        super().__init__("Instagram publishing is unavailable.")


@dataclass(frozen=True)
class ClaimedScheduledContent:
    id: uuid.UUID
    instagram_account_id: uuid.UUID
    instagram_user_id: str
    content_type: str
    caption: str
    media_url: str


@dataclass(frozen=True)
class ContentPublishingSummary:
    claimed: int = 0
    published: int = 0
    failed: int = 0
    skipped: int = 0


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _safe_code(value: object) -> str:
    return value if isinstance(value, str) and value in SAFE_ERROR_CODES else "unexpected_publish_error"


def _safe_provider_diagnostics(value: Mapping[str, object] | None) -> dict[str, object]:
    """Keep only bounded, non-secret provider rejection diagnostics."""

    if not isinstance(value, Mapping):
        return {}
    diagnostics: dict[str, object] = {}
    stage = value.get("publish_stage")
    if isinstance(stage, str) and stage in _SAFE_PUBLISH_STAGES:
        diagnostics["publish_stage"] = stage
    status = value.get("provider_http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostics["provider_http_status"] = status
    classification = value.get("provider_error_classification")
    if isinstance(classification, str) and classification in _SAFE_PROVIDER_ERROR_CLASSIFICATIONS:
        diagnostics["provider_error_classification"] = classification
    error_type = value.get("provider_error_type")
    if isinstance(error_type, str) and _SAFE_PROVIDER_ERROR_TYPES.fullmatch(error_type):
        diagnostics["provider_error_type"] = error_type
    for key in ("provider_error_code", "provider_error_subcode"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 2_147_483_647:
            diagnostics[key] = number
    container_status = value.get("provider_container_status")
    if isinstance(container_status, str) and container_status in _SAFE_CONTAINER_STATUSES:
        diagnostics["provider_container_status"] = container_status
    return diagnostics


def _provider_rejection(stage: str, response: object) -> InstagramPublishError:
    """Classify one provider non-2xx response without retaining its body."""

    diagnostics: dict[str, object] = {
        "publish_stage": stage,
        "provider_error_classification": "provider_error_unparseable",
    }
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        diagnostics["provider_http_status"] = status
    try:
        payload = response.json()  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return InstagramPublishError(
            "provider_rejected",
            request_may_have_been_sent=False,
            safe_diagnostics=diagnostics,
        )
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(error, Mapping):
        return InstagramPublishError(
            "provider_rejected",
            request_may_have_been_sent=False,
            safe_diagnostics=diagnostics,
        )
    diagnostics["provider_error_classification"] = "provider_error_metadata"
    for source_key, diagnostic_key in (
        ("type", "provider_error_type"),
        ("code", "provider_error_code"),
        ("error_subcode", "provider_error_subcode"),
    ):
        if source_key in error:
            diagnostics[diagnostic_key] = error[source_key]
    return InstagramPublishError(
        "provider_rejected",
        request_may_have_been_sent=False,
        safe_diagnostics=diagnostics,
    )


def _provider_rejection_log_message(diagnostics: Mapping[str, object]) -> str:
    """Render only allowlisted diagnostics because Railway drops log extras."""

    safe = _safe_provider_diagnostics(diagnostics)
    return (
        "scheduled_content_publishing_failed "
        f"publish_stage={safe.get('publish_stage')} "
        f"provider_http_status={safe.get('provider_http_status')} "
        f"provider_error_classification={safe.get('provider_error_classification')} "
        f"provider_error_type={safe.get('provider_error_type')} "
        f"provider_error_code={safe.get('provider_error_code')} "
        f"provider_error_subcode={safe.get('provider_error_subcode')} "
        f"provider_container_status={safe.get('provider_container_status')}"
    )


def claim_due_scheduled_content(
    session: Session, *, now: datetime | None = None, limit: int = 25
) -> list[ClaimedScheduledContent]:
    """Atomically transition due scheduled rows to publishing under a row lock.

    The caller owns the surrounding short transaction and must commit it before
    making any provider request.
    """

    current = _now(now)
    bounded_limit = max(1, min(int(limit), 100))
    items = list(
        session.scalars(
            select(InstagramContentItem)
            .where(
                InstagramContentItem.status == "scheduled",
                InstagramContentItem.scheduled_for.is_not(None),
                InstagramContentItem.scheduled_for <= current,
            )
            .order_by(InstagramContentItem.scheduled_for, InstagramContentItem.id)
            .limit(bounded_limit)
            .with_for_update(skip_locked=True)
        ).all()
    )
    claimed: list[ClaimedScheduledContent] = []
    for item in items:
        # Recheck while holding the lock so a concurrent state transition wins.
        if item.status != "scheduled" or item.scheduled_for is None:
            LOGGER.info("scheduled_content_skipped", extra={"content_id": str(item.id), "reason": "no_longer_eligible"})
            continue
        item.status = "publishing"
        item.publish_error_code = None
        item.provider_request_may_have_been_sent = False
        account = item.instagram_account
        claimed.append(
            ClaimedScheduledContent(
                id=item.id,
                instagram_account_id=item.instagram_account_id,
                instagram_user_id=str(account.instagram_user_id),
                content_type=str(item.content_type),
                caption=str(item.caption),
                media_url=str(item.media_url),
            )
        )
        LOGGER.info("scheduled_content_selected", extra={"content_id": str(item.id), "instagram_account_id": str(item.instagram_account_id)})
    session.flush()
    return claimed


def _claimed_item(
    session: Session, *, content_id: object, instagram_account_id: object
) -> InstagramContentItem | None:
    return session.scalar(
        select(InstagramContentItem)
        .where(
            InstagramContentItem.id == content_id,
            InstagramContentItem.instagram_account_id == instagram_account_id,
            InstagramContentItem.status == "publishing",
        )
        .with_for_update()
    )


def mark_content_published(
    session: Session,
    *,
    content_id: object,
    instagram_account_id: object,
    provider_media_id: object,
    now: datetime | None = None,
) -> bool:
    """Persist a successful claimed publication once; terminal rows are untouched."""

    media_id = str(provider_media_id or "").strip()
    if not media_id or len(media_id) > 255:
        return False
    item = _claimed_item(session, content_id=content_id, instagram_account_id=instagram_account_id)
    if item is None:
        return False
    item.status = "published"
    item.provider_media_id = media_id
    item.published_at = _now(now)
    item.publish_error_code = None
    item.provider_request_may_have_been_sent = False
    session.flush()
    return True


def mark_content_publish_failed(
    session: Session,
    *,
    content_id: object,
    instagram_account_id: object,
    error_code: object,
    provider_request_may_have_been_sent: bool,
) -> bool:
    """Persist a sanitized terminal failure for one still-claimed item."""

    item = _claimed_item(session, content_id=content_id, instagram_account_id=instagram_account_id)
    if item is None:
        return False
    item.status = "failed"
    item.publish_error_code = _safe_code(error_code)
    item.provider_request_may_have_been_sent = bool(provider_request_may_have_been_sent)
    session.flush()
    return True


def _payload_id(response: object) -> str | None:
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    value = payload.get("id") if isinstance(payload, dict) else None
    identifier = str(value or "").strip()
    return identifier if identifier else None


def _provider_post(path: str, runtime: InstagramAccountRuntime, data: dict[str, str]) -> Any:
    try:
        return requests.post(
            f"{GRAPH_ROOT}/{API_VERSION}/{path.lstrip('/')}",
            data=data,
            headers={"Authorization": f"Bearer {runtime.access_token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise InstagramPublishError(
            "provider_request_ambiguous", request_may_have_been_sent=True
        ) from exc


def _provider_get(path: str, runtime: InstagramAccountRuntime, params: dict[str, str]) -> Any:
    """Read provider state; this request cannot create a publication."""

    try:
        return requests.get(
            f"{GRAPH_ROOT}/{API_VERSION}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {runtime.access_token}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise InstagramPublishError(
            "provider_container_not_ready",
            request_may_have_been_sent=False,
            safe_diagnostics={
                "publish_stage": "container_status",
                "provider_error_classification": "provider_error_unparseable",
            },
        ) from exc


def _container_status(response: object) -> str | None:
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return None
    value = payload.get("status_code") if isinstance(payload, Mapping) else None
    return value if isinstance(value, str) and value in _SAFE_CONTAINER_STATUSES else None


def _wait_for_container_ready(runtime: InstagramAccountRuntime, creation_id: str) -> None:
    """Poll one existing container with fixed bounds; never create another."""

    last_status: str | None = None
    for attempt in range(CONTAINER_STATUS_MAX_ATTEMPTS):
        response = _provider_get(creation_id, runtime, {"fields": "status_code"})
        if not bool(getattr(response, "ok", False)):
            raise _provider_rejection("container_status", response)
        last_status = _container_status(response)
        diagnostics = {
            "publish_stage": "container_status",
            "provider_container_status": last_status,
        }
        if last_status == "FINISHED":
            return
        if last_status in {"ERROR", "EXPIRED", "PUBLISHED"}:
            raise InstagramPublishError(
                "provider_container_failed",
                request_may_have_been_sent=False,
                safe_diagnostics=diagnostics,
            )
        if attempt + 1 < CONTAINER_STATUS_MAX_ATTEMPTS:
            time.sleep(CONTAINER_STATUS_POLL_INTERVAL_SECONDS)
    raise InstagramPublishError(
        "provider_container_not_ready",
        request_may_have_been_sent=False,
        safe_diagnostics={
            "publish_stage": "container_status",
            "provider_container_status": last_status,
        },
    )


def publish_instagram_image(runtime: InstagramAccountRuntime, item: ClaimedScheduledContent) -> str:
    """Publish one image through the tenant-bound Graph API runtime.

    A timeout or malformed response after the publish request is deliberately
    ambiguous. The caller records that state but never automatically resends.
    """

    container = _provider_post(
        f"{runtime.instagram_user_id}/media",
        runtime,
        {"image_url": item.media_url, "caption": item.caption},
    )
    if not bool(getattr(container, "ok", False)):
        raise _provider_rejection("container_create", container)
    creation_id = _payload_id(container)
    if creation_id is None:
        raise InstagramPublishError("provider_response_invalid", request_may_have_been_sent=False)

    _wait_for_container_ready(runtime, creation_id)

    published = _provider_post(
        f"{runtime.instagram_user_id}/media_publish",
        runtime,
        {"creation_id": creation_id},
    )
    if not bool(getattr(published, "ok", False)):
        raise _provider_rejection("media_publish", published)
    media_id = _payload_id(published)
    if media_id is None:
        raise InstagramPublishError("provider_response_invalid", request_may_have_been_sent=True)
    return media_id


def _record_failure(
    session_factory: Callable[[], Session], item: ClaimedScheduledContent, error_code: str, ambiguous: bool
) -> bool:
    with session_factory.begin() as session:
        return mark_content_publish_failed(
            session,
            content_id=item.id,
            instagram_account_id=item.instagram_account_id,
            error_code=error_code,
            provider_request_may_have_been_sent=ambiguous,
        )


def execute_due_scheduled_content(
    session_factory: Callable[[], Session],
    *,
    now: datetime | None = None,
    limit: int = 25,
    runtime_resolver: Callable[..., InstagramAccountRuntime] = resolve_oauth_instagram_account_runtime,
    publisher: Callable[[InstagramAccountRuntime, ClaimedScheduledContent], str] = publish_instagram_image,
) -> ContentPublishingSummary:
    """Claim then execute a bounded batch without automatically retrying failures."""

    current = _now(now)
    with session_factory.begin() as session:
        claimed = claim_due_scheduled_content(session, now=current, limit=limit)

    published_count = failed_count = skipped_count = 0
    for item in claimed:
        LOGGER.info("scheduled_content_publishing_started", extra={"content_id": str(item.id), "instagram_account_id": str(item.instagram_account_id)})
        if item.content_type != IMAGE_CONTENT_TYPE:
            failed_count += int(_record_failure(session_factory, item, "unsupported_content_type", False))
            LOGGER.info("scheduled_content_publishing_failed", extra={"content_id": str(item.id), "error_code": "unsupported_content_type", "provider_request_may_have_been_sent": False})
            continue
        try:
            with session_factory() as session:
                runtime = runtime_resolver(
                    session,
                    instagram_account_id=item.instagram_account_id,
                    instagram_user_id=item.instagram_user_id,
                    now=current,
                )
            if (
                runtime.instagram_account_id != item.instagram_account_id
                or runtime.instagram_user_id != item.instagram_user_id
                or runtime.connection_mode != "oauth"
            ):
                raise InstagramPublishError("tenant_identity_mismatch", request_may_have_been_sent=False)
            provider_media_id = publisher(runtime, item)
            with session_factory.begin() as session:
                completed = mark_content_published(
                    session,
                    content_id=item.id,
                    instagram_account_id=item.instagram_account_id,
                    provider_media_id=provider_media_id,
                    now=current,
                )
            if completed:
                published_count += 1
                LOGGER.info("scheduled_content_publishing_succeeded", extra={"content_id": str(item.id), "instagram_account_id": str(item.instagram_account_id), "provider_media_id": provider_media_id})
            else:
                skipped_count += 1
                LOGGER.info("scheduled_content_skipped", extra={"content_id": str(item.id), "reason": "no_longer_eligible"})
        except RuntimeCredentialUnavailableError:
            failed_count += int(_record_failure(session_factory, item, "instagram_connection_unavailable", False))
            LOGGER.info("scheduled_content_publishing_failed", extra={"content_id": str(item.id), "error_code": "instagram_connection_unavailable", "provider_request_may_have_been_sent": False})
        except InstagramPublishError as error:
            failed_count += int(_record_failure(session_factory, item, error.code, error.request_may_have_been_sent))
            if error.safe_diagnostics:
                LOGGER.warning(_provider_rejection_log_message(error.safe_diagnostics))
            else:
                LOGGER.info("scheduled_content_publishing_failed", extra={"content_id": str(item.id), "error_code": error.code, "provider_request_may_have_been_sent": error.request_may_have_been_sent})
        except Exception:
            failed_count += int(_record_failure(session_factory, item, "unexpected_publish_error", True))
            LOGGER.info("scheduled_content_publishing_failed", extra={"content_id": str(item.id), "error_code": "unexpected_publish_error", "provider_request_may_have_been_sent": True})

    return ContentPublishingSummary(
        claimed=len(claimed), published=published_count, failed=failed_count, skipped=skipped_count
    )
