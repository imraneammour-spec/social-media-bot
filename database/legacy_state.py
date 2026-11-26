"""Durable PostgreSQL backing for the legacy bot's load-bearing JSON state.

The legacy bot kept its cooldown/dedupe/pending-DM/prompt-cache state in
JSON files under ``DATA_DIR`` (``processed_comments.json``,
``requests.json``, ``pending_access.json``, ``posts.json``). On Heroku that
directory is ephemeral: every deploy or dyno restart wiped the state, so
the bot could reply twice to the same comment, lose the 10-minute cooldown,
and re-extract prompts (burning Gemini quota).

This module stores the same state in Postgres, scoped to the legacy
Instagram account row (``connection_status="environment_managed"``,
created by ``bootstrap-owner``). Every method returns the same shapes as
the JSON helpers in ``webhook.py`` so the swap is behavior-identical.

Failure policy: any database error raises :class:`LegacyStateError` and
the caller falls back to the JSON files, so a database hiccup can never
break the webhook. When no session factory is configured (tests, local
dev) or the legacy account row does not exist yet, ``legacy_state_store``
returns ``None`` and the JSON files are used.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as lite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .models import (
    InstagramAccount,
    LegacyPendingAccess,
    LegacyProcessedComment,
    LegacyPromptPost,
    LegacyPromptRequest,
)

MAX_PROCESSED_COMMENTS = 2000


def _insert_for(session: Session) -> Any:
    """Dialect-aware INSERT (Postgres in production, SQLite in tests)."""

    bind = session.get_bind()
    name = getattr(getattr(bind, "dialect", None), "name", "")
    return pg_insert if name == "postgresql" else lite_insert
MAX_PROMPT_REQUESTS = 5000


class LegacyStateError(ValueError):
    """Raised without secret material when durable legacy state is unavailable."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _prune_oldest(session: Session, model: Any, account_id: uuid.UUID, keep: int) -> None:
    """Delete all but the newest ``keep`` rows for one account (by age)."""

    count = session.scalar(
        select(func.count())
        .select_from(model)
        .where(model.instagram_account_id == account_id)
    )
    if not count or count <= keep:
        return
    excess = count - keep
    oldest_ids = session.scalars(
        select(model.id)
        .where(model.instagram_account_id == account_id)
        .order_by(model.created_at.asc(), model.id.asc())
        .limit(excess)
    ).all()
    if oldest_ids:
        session.execute(delete(model).where(model.id.in_(oldest_ids)))


class LegacyStateStore:
    """Postgres-backed equivalent of the legacy JSON state helpers."""

    def __init__(self, session_factory: Any, instagram_user_id: str) -> None:
        self._session_factory = session_factory
        self._instagram_user_id = str(instagram_user_id or "").strip()
        self._account_id: uuid.UUID | None = None
        self._account_missing = False

    @contextmanager
    def _session(self) -> Any:
        try:
            session = self._session_factory()
        except Exception as error:
            raise LegacyStateError("Legacy state is unavailable.") from error
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _resolve_account_id(self, session: Session) -> uuid.UUID | None:
        if self._account_id is not None:
            return self._account_id
        if self._account_missing or not self._instagram_user_id:
            return None
        account = session.scalar(
            select(InstagramAccount).where(
                InstagramAccount.instagram_user_id == self._instagram_user_id
            )
        )
        if account is None:
            self._account_missing = True
            return None
        self._account_id = account.id
        return account.id

    def available(self) -> bool:
        """True when the legacy account row exists (probe, no writes)."""

        try:
            with self._session() as session:
                return self._resolve_account_id(session) is not None
        except (SQLAlchemyError, AttributeError, TypeError, ValueError):
            return False

    # -- processed comments (dedupe) -------------------------------------

    def load_processed_comments(self) -> dict[str, str]:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                rows = session.execute(
                    select(
                        LegacyProcessedComment.comment_id, LegacyProcessedComment.state
                    ).where(
                        LegacyProcessedComment.instagram_account_id == account_id
                    )
                ).all()
                return {str(comment_id): str(state) for comment_id, state in rows}
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def claim_comment(self, comment_id: str) -> bool:
        """Atomically claim a comment; False when it was already claimed."""

        comment_id = str(comment_id)
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                statement = (
                    _insert_for(session)(LegacyProcessedComment)
                    .values(
                        instagram_account_id=account_id,
                        comment_id=comment_id,
                        state="processing",
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                    .on_conflict_do_nothing(
                        index_elements=["instagram_account_id", "comment_id"]
                    )
                )
                result = session.execute(statement)
                if result.rowcount == 0:
                    return False
                _prune_oldest(
                    session, LegacyProcessedComment, account_id, MAX_PROCESSED_COMMENTS
                )
                return True
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def mark_comment_done(self, comment_id: str) -> None:
        comment_id = str(comment_id)
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                statement = (
                    _insert_for(session)(LegacyProcessedComment)
                    .values(
                        instagram_account_id=account_id,
                        comment_id=comment_id,
                        state="done",
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                    .on_conflict_do_update(
                        index_elements=["instagram_account_id", "comment_id"],
                        set_={"state": "done", "updated_at": _utcnow()},
                    )
                )
                session.execute(statement)
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def release_comment(self, comment_id: str) -> None:
        comment_id = str(comment_id)
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                session.execute(
                    delete(LegacyProcessedComment).where(
                        LegacyProcessedComment.instagram_account_id == account_id,
                        LegacyProcessedComment.comment_id == comment_id,
                    )
                )
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    # -- prompt posts (prompt cache) --------------------------------------

    def load_posts(self) -> dict[str, dict[str, Any]]:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                rows = session.scalars(
                    select(LegacyPromptPost).where(
                        LegacyPromptPost.instagram_account_id == account_id
                    )
                ).all()
                return {row.media_id: self._post_shape(row) for row in rows}
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    @staticmethod
    def _post_shape(row: LegacyPromptPost) -> dict[str, Any]:
        return {
            "media_id": str(row.media_id),
            "permalink": row.permalink or "",
            "cover_url": row.cover_url or "",
            "prompts": row.prompts or [],
            "created_at": _iso(row.created_at),
            "prompt_count": int(row.prompt_count or 0),
        }

    def get_saved_post(self, media_id: str) -> dict[str, Any] | None:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                row = session.scalar(
                    select(LegacyPromptPost).where(
                        LegacyPromptPost.instagram_account_id == account_id,
                        LegacyPromptPost.media_id == str(media_id),
                    )
                )
                return self._post_shape(row) if row is not None else None
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def save_post(
        self,
        media_id: str,
        permalink: str,
        prompts: list[dict[str, Any]],
        cover_url: str = "",
    ) -> None:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                now = _utcnow()
                statement = (
                    _insert_for(session)(LegacyPromptPost)
                    .values(
                        instagram_account_id=account_id,
                        media_id=str(media_id),
                        permalink=permalink or "",
                        cover_url=cover_url or "",
                        prompts=list(prompts),
                        prompt_count=len(prompts),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["instagram_account_id", "media_id"],
                        set_={
                            "permalink": permalink or "",
                            "cover_url": cover_url or "",
                            "prompts": list(prompts),
                            "prompt_count": len(prompts),
                            "updated_at": now,
                        },
                    )
                )
                session.execute(statement)
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    # -- prompt requests (cooldown log) -----------------------------------

    def load_requests(self) -> list[dict[str, Any]]:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                rows = session.scalars(
                    select(LegacyPromptRequest)
                    .where(LegacyPromptRequest.instagram_account_id == account_id)
                    .order_by(
                        LegacyPromptRequest.created_at.asc(),
                        LegacyPromptRequest.id.asc(),
                    )
                    .limit(MAX_PROMPT_REQUESTS)
                ).all()
                return [self._request_shape(row) for row in rows]
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    @staticmethod
    def _request_shape(row: LegacyPromptRequest) -> dict[str, Any]:
        return {
            "request_key": row.request_key,
            "media_id": row.media_id,
            "comment_id": row.comment_id,
            "commenter_id": row.commenter_id or "",
            "username": row.username or "",
            "page_url": row.page_url or "",
            "created_at": _iso(row.created_at),
        }

    def latest_request_for_key(self, request_key: str) -> dict[str, Any] | None:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                row = session.scalar(
                    select(LegacyPromptRequest)
                    .where(
                        LegacyPromptRequest.instagram_account_id == account_id,
                        LegacyPromptRequest.request_key == str(request_key),
                    )
                    .order_by(
                        LegacyPromptRequest.created_at.desc(),
                        LegacyPromptRequest.id.desc(),
                    )
                    .limit(1)
                )
                return self._request_shape(row) if row is not None else None
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def log_prompt_request(self, entry: dict[str, Any]) -> None:
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                session.add(
                    LegacyPromptRequest(
                        instagram_account_id=account_id,
                        request_key=str(entry.get("request_key") or ""),
                        media_id=str(entry.get("media_id") or ""),
                        comment_id=str(entry.get("comment_id") or ""),
                        commenter_id=str(entry.get("commenter_id") or ""),
                        username=str(entry.get("username") or ""),
                        page_url=str(entry.get("page_url") or ""),
                        created_at=_utcnow(),
                        updated_at=_utcnow(),
                    )
                )
                session.flush()
                _prune_oldest(
                    session, LegacyPromptRequest, account_id, MAX_PROMPT_REQUESTS
                )
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    # -- pending access (DM waiting list) ---------------------------------

    def save_pending_access(self, user_id: str, page_url: str) -> bool:
        if not user_id or not page_url:
            return False
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                now = _utcnow()
                statement = (
                    _insert_for(session)(LegacyPendingAccess)
                    .values(
                        instagram_account_id=account_id,
                        recipient_id=str(user_id),
                        page_url=str(page_url),
                        created_at=now,
                        updated_at=now,
                    )
                    .on_conflict_do_update(
                        index_elements=["instagram_account_id", "recipient_id"],
                        set_={"page_url": str(page_url), "updated_at": now},
                    )
                )
                session.execute(statement)
                return True
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def get_pending_access(self, user_id: str) -> dict[str, Any] | None:
        if not user_id:
            return None
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                row = session.scalar(
                    select(LegacyPendingAccess).where(
                        LegacyPendingAccess.instagram_account_id == account_id,
                        LegacyPendingAccess.recipient_id == str(user_id),
                    )
                )
                if row is None:
                    return None
                return {"page_url": row.page_url, "created_at": _iso(row.created_at)}
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error

    def clear_pending_access(self, user_id: str) -> None:
        if not user_id:
            return
        try:
            with self._session() as session:
                account_id = self._resolve_account_id(session)
                if account_id is None:
                    raise LegacyStateError("Legacy account is unavailable.")
                session.execute(
                    delete(LegacyPendingAccess).where(
                        LegacyPendingAccess.instagram_account_id == account_id,
                        LegacyPendingAccess.recipient_id == str(user_id),
                    )
                )
        except LegacyStateError:
            raise
        except (SQLAlchemyError, AttributeError, TypeError, ValueError) as error:
            raise LegacyStateError("Legacy state is unavailable.") from error


def legacy_state_store(session_factory: Any, instagram_user_id: str) -> LegacyStateStore | None:
    """Return a Postgres-backed store, or None to keep using the JSON files."""

    if session_factory is None or not str(instagram_user_id or "").strip():
        return None
    store = LegacyStateStore(session_factory, instagram_user_id)
    try:
        if not store.available():
            return None
    except (SQLAlchemyError, AttributeError, TypeError, ValueError):
        return None
    return store
