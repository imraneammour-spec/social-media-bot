"""SQLAlchemy schema definitions for the additive database foundation."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid


class Base(DeclarativeBase):
    """Base metadata used by Alembic; importing it does not create tables."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(TimestampMixin, Base):
    """Future application identity; no authentication is implemented in Phase 2."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    instagram_accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class InstagramAccount(TimestampMixin, Base):
    """An Instagram account owned by exactly one future application user."""

    __tablename__ = "instagram_accounts"
    __table_args__ = (
        Index("ix_instagram_accounts_user_connection", "user_id", "connection_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    instagram_user_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="instagram", server_default="instagram"
    )
    connection_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="environment_managed"
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_connection_error_code: Mapped[str | None] = mapped_column(String(128))

    owner: Mapped[User] = relationship(back_populates="instagram_accounts")
    credentials: Mapped[list["InstagramCredential"]] = relationship(
        back_populates="instagram_account", cascade="all, delete-orphan"
    )


class InstagramCredential(TimestampMixin, Base):
    """Future encrypted per-account credential storage.

    This table is intentionally empty in Phase 2.  The production access token
    remains only in ``INSTAGRAM_ACCESS_TOKEN`` and no token is copied here.
    """

    __tablename__ = "instagram_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    token_ciphertext: Mapped[str | None] = mapped_column(Text)
    encryption_key_id: Mapped[str | None] = mapped_column(String(255))
    scopes: Mapped[list[str] | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_status: Mapped[str | None] = mapped_column(String(32))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))

    instagram_account: Mapped[InstagramAccount] = relationship(back_populates="credentials")


class MigrationRun(TimestampMixin, Base):
    """Auditable record for a future JSON-to-PostgreSQL migration, not data itself."""

    __tablename__ = "migration_runs"
    __table_args__ = (
        UniqueConstraint("migration_name", "source", name="uq_migration_runs_name_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    migration_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    source_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column("metadata", JSON)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
