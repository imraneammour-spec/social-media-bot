"""SQLAlchemy schema definitions for the additive database foundation."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
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
    notification_preferences: Mapped[dict[str, object] | None] = mapped_column(
        JSON, default=dict, server_default=text("'{}'")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    instagram_accounts: Mapped[list["InstagramAccount"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    onboarding_profile: Mapped["UserOnboardingProfile | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class UserOnboardingProfile(TimestampMixin, Base):
    """Allowlisted onboarding preferences for one newly registered user."""

    __tablename__ = "user_onboarding_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    use_case: Mapped[str | None] = mapped_column(String(64))
    main_goal: Mapped[str | None] = mapped_column(String(64))
    onboarding_source: Mapped[str | None] = mapped_column(String(64))
    selected_plan: Mapped[str | None] = mapped_column(String(32))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="onboarding_profile")


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
    automation_config: Mapped["InstagramAutomationConfig | None"] = relationship(
        back_populates="instagram_account", cascade="all, delete-orphan", uselist=False
    )
    content_items: Mapped[list["InstagramContentItem"]] = relationship(
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


class InstagramAutomationConfig(TimestampMixin, Base):
    """Tenant-owned automation choices for one OAuth Instagram account."""

    __tablename__ = "instagram_automation_configs"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", name="uq_automation_config_account"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    trigger_keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    public_reply_enabled: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    public_reply_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    delivery_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="prompt_request", server_default=text("'prompt_request'")
    )
    private_message_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    prompt_opening_message_text: Mapped[str | None] = mapped_column(Text)
    destination_url: Mapped[str | None] = mapped_column(Text)
    inactive_mode_settings: Mapped[dict[str, object] | None] = mapped_column(JSON)
    followup_enabled: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    followup_delay_minutes: Mapped[int] = mapped_column(
        nullable=False, default=360, server_default=text("360")
    )
    followup_message_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=text("''")
    )
    smart_reply_enabled: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    smart_reply_tone: Mapped[str] = mapped_column(
        String(32), nullable=False, default="friendly", server_default=text("'friendly'")
    )
    smart_reply_max_per_hour: Mapped[int] = mapped_column(
        nullable=False, default=20, server_default=text("20")
    )

    instagram_account: Mapped[InstagramAccount] = relationship(
        back_populates="automation_config"
    )


class InstagramEngagementEvent(TimestampMixin, Base):
    """Durable OAuth-only webhook claim; legacy JSON dedupe remains unchanged."""

    __tablename__ = "instagram_engagement_events"
    __table_args__ = (
        UniqueConstraint("instagram_account_id", "provider_event_id", "event_type", name="uq_engagement_event_account_provider_type"),
        Index("ix_engagement_events_account_state", "instagram_account_id", "state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="processing", server_default="processing"
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstagramContentItem(TimestampMixin, Base):
    """Minimal account-scoped content drafts and ready-to-publish assets."""

    __tablename__ = "instagram_content_items"
    __table_args__ = (
        Index("ix_content_items_account_status", "instagram_account_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", server_default="draft")
    content_type: Mapped[str] = mapped_column(String(32), nullable=False, default="image", server_default="image")
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    media_url: Mapped[str] = mapped_column(Text, nullable=False)
    cover_url: Mapped[str | None] = mapped_column(Text)
    alt_text: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, object] | None] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_media_id: Mapped[str | None] = mapped_column(String(255))
    publish_error_code: Mapped[str | None] = mapped_column(String(128))
    provider_request_may_have_been_sent: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )

    instagram_account: Mapped[InstagramAccount] = relationship(back_populates="content_items")


class InstagramPromptPost(TimestampMixin, Base):
    """OAuth prompt content, scoped to one connected Instagram account."""

    __tablename__ = "instagram_prompt_posts"
    __table_args__ = (
        UniqueConstraint("instagram_account_id", "media_id", name="uq_prompt_post_account_media"),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    media_id: Mapped[str] = mapped_column(String(255), nullable=False)
    public_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    prompts: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    permalink: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)


class InstagramPendingAccess(TimestampMixin, Base):
    __tablename__ = "instagram_pending_access"
    __table_args__ = (UniqueConstraint("instagram_account_id", "recipient_id", name="uq_pending_access_account_recipient"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    page_url: Mapped[str] = mapped_column(Text, nullable=False)


class InstagramPromptRequest(TimestampMixin, Base):
    __tablename__ = "instagram_prompt_requests"
    __table_args__ = (
        Index("ix_prompt_requests_account_media_recipient", "instagram_account_id", "media_id", "recipient_id"),
        Index(
            "ix_prompt_requests_account_lead_state_created",
            "instagram_account_id",
            "lead_archived_at",
            "lead_status",
            "created_at",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    media_id: Mapped[str] = mapped_column(String(255), nullable=False)
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    page_url: Mapped[str] = mapped_column(Text, nullable=False)
    lead_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="new", server_default="new"
    )
    internal_note: Mapped[str | None] = mapped_column(Text)
    lead_archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ai_intent: Mapped[str | None] = mapped_column(String(32))
    ai_priority: Mapped[str | None] = mapped_column(String(16))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    ai_suggested_followup: Mapped[str | None] = mapped_column(Text)
    ai_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstagramSmartReplyActivity(TimestampMixin, Base):
    __tablename__ = "instagram_smart_reply_activity"
    __table_args__ = (Index("ix_smart_reply_activity_account_created", "instagram_account_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False)
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_id: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255))
    reply_text: Mapped[str] = mapped_column(Text, nullable=False)


class InstagramOAuthFollowupJob(TimestampMixin, Base):
    """Durable delayed OAuth message work; legacy follow-up JSON is untouched."""

    __tablename__ = "instagram_oauth_followup_jobs"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "source_message_id",
            name="uq_oauth_followup_account_source_message",
        ),
        Index(
            "uq_oauth_followup_active_account_recipient",
            "instagram_account_id", "recipient_id",
            unique=True,
            postgresql_where=text("state IN ('pending', 'processing')"),
            sqlite_where=text("state IN ('pending', 'processing')"),
        ),
        Index("ix_oauth_followup_state_run_at", "state", "run_at"),
        Index(
            "ix_oauth_followup_account_recipient",
            "instagram_account_id", "recipient_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    prompt_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("instagram_prompt_requests.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    page_url: Mapped[str | None] = mapped_column(Text)
    message_text: Mapped[str | None] = mapped_column(Text)
    provider_window_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128))


class InstagramOAuthDirectMessageInteraction(TimestampMixin, Base):
    """One initial direct-message reply that may qualify one later user reply."""

    __tablename__ = "instagram_oauth_direct_message_interactions"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "source_comment_id",
            name="uq_oauth_direct_interaction_account_source_comment",
        ),
        Index(
            "ix_oauth_direct_interaction_account_recipient_reply",
            "instagram_account_id", "recipient_id", "recipient_reply_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    source_comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    initial_private_reply_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recipient_reply_message_id: Mapped[str | None] = mapped_column(String(255))
    recipient_reply_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InstagramOAuthWebhookWorkItem(TimestampMixin, Base):
    """Minimal durable OAuth webhook ingress, before engagement processing."""

    __tablename__ = "instagram_oauth_webhook_work_items"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "provider_event_id", "event_type",
            name="uq_oauth_webhook_work_account_event_type",
        ),
        Index("ix_oauth_webhook_work_state_lease", "state", "lease_expires_at"),
        Index("ix_oauth_webhook_work_account_state", "instagram_account_id", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False
    )
    instagram_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    public_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


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


class BillingSubscription(TimestampMixin, Base):
    __tablename__ = "billing_subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    provider_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="free")
    interval: Mapped[str | None] = mapped_column(String(16))
    test_mode: Mapped[bool] = mapped_column(nullable=False, default=True)
    renews_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BillingCheckout(TimestampMixin, Base):
    __tablename__ = "billing_checkouts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    test_mode: Mapped[bool] = mapped_column(nullable=False)
    consumed_by: Mapped[str | None] = mapped_column(String(64))


class BillingWebhook(Base):
    __tablename__ = "billing_webhooks"

    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_name: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BillingUsage(Base):
    __tablename__ = "billing_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class LegacyProcessedComment(TimestampMixin, Base):
    """Durable legacy-bot comment dedupe claim (replaces processed_comments.json).

    One row per comment the legacy bot has seen; ``state`` is ``processing``
    while the comment is being handled and ``done`` afterwards. The JSON file
    is wiped on every Heroku restart, which allowed double replies; this
    table survives restarts. Scoped to the legacy Instagram account.
    """

    __tablename__ = "legacy_processed_comments"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "comment_id", name="uq_legacy_processed_account_comment"
        ),
        Index("ix_legacy_processed_account_created", "instagram_account_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="processing", server_default="processing"
    )


class LegacyPromptRequest(TimestampMixin, Base):
    """Durable legacy-bot prompt-request log (replaces requests.json).

    One row per successful prompt-request flow; the latest row for a
    ``request_key`` drives the 10-minute cooldown. Kept to the newest 5000
    rows, mirroring the JSON cap.
    """

    __tablename__ = "legacy_prompt_requests"
    __table_args__ = (
        Index(
            "ix_legacy_prompt_requests_account_key_created",
            "instagram_account_id",
            "request_key",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_key: Mapped[str] = mapped_column(String(512), nullable=False)
    media_id: Mapped[str] = mapped_column(String(255), nullable=False)
    comment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    commenter_id: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    username: Mapped[str | None] = mapped_column(String(255))
    page_url: Mapped[str] = mapped_column(Text, nullable=False, default="")


class LegacyPendingAccess(TimestampMixin, Base):
    """Durable legacy-bot pending DM access (replaces pending_access.json).

    One row per user waiting for the prompt page DM; cleared when the DM is
    sent or the user replies. Scoped to the legacy Instagram account.
    """

    __tablename__ = "legacy_pending_access"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "recipient_id", name="uq_legacy_pending_account_recipient"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    recipient_id: Mapped[str] = mapped_column(String(255), nullable=False)
    page_url: Mapped[str] = mapped_column(Text, nullable=False)


class LegacyPromptPost(TimestampMixin, Base):
    """Durable legacy-bot prompt cache (replaces posts.json).

    One row per media item whose prompts were extracted by Gemini, so the
    same post is never re-extracted after a restart. Mirrors the JSON shape
    (media_id, permalink, cover_url, prompts, prompt_count).
    """

    __tablename__ = "legacy_prompt_posts"
    __table_args__ = (
        UniqueConstraint(
            "instagram_account_id", "media_id", name="uq_legacy_prompt_post_account_media"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instagram_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("instagram_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permalink: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    prompts: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    prompt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserGeminiKey(TimestampMixin, Base):
    """One BYOK Gemini API key per user, Fernet-encrypted (see gemini_keys.py).

    When a user has an active key, their Gemini calls use it instead of the
    shared ``GEMINI_API_KEY`` env key; when they have none, the shared key
    is the fallback. ``key_hint`` holds only the last 4 characters for
    display (never enough to reconstruct the key).
    """

    __tablename__ = "user_gemini_keys"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key_ciphertext: Mapped[str | None] = mapped_column(Text)
    encryption_key_id: Mapped[str | None] = mapped_column(String(255))
    key_hint: Mapped[str | None] = mapped_column(String(8))
    key_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", server_default="active"
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
