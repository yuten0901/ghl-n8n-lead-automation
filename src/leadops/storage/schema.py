"""Database schema.

Four tables, each earning its place:

* `webhook_events` — the idempotency ledger. The UNIQUE constraint on
  `idempotency_key` is the actual concurrency control: two simultaneous
  deliveries race to INSERT, one wins, the loser reads the winner's row.
  Nothing here relies on "check then act", which is the classic broken pattern.
* `leads` — one row per *person* per location, keyed by `identity_key`.
  Holds the GHL contact id so a returning lead never creates a second contact.
* `audit_log` — append-only record of every step of every event, for the
  "why did this lead get routed there?" question that always arrives later.
* `dead_letters` — events that exhausted retries or failed permanently, with
  enough of the original request to replay them by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class UTCDateTime(TypeDecorator):
    """A timestamp that is timezone-aware UTC on the way in *and on the way out*.

    SQLite has no timezone type: `DateTime(timezone=True)` stores the value and
    hands it back **naive**, so `stored <= datetime.now(tz=utc)` raises
    TypeError. Postgres returns it aware, so the bug is invisible until the
    portable demo path runs - which is exactly where it bit here, in the
    lease-expiry check on the concurrent-delivery path.

    Normalising in one place means no caller has to remember which backend it is
    talking to.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # noqa: ANN001
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class WebhookEvent(Base):
    """One inbound webhook delivery."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    key_derivation: Mapped[str] = mapped_column(String(40), default="")
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True, default="received")

    signature_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # The response returned for the first successful processing. Replayed verbatim
    # for later deliveries of the same event so the caller sees a stable answer.
    stored_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    lead_identity_key: Mapped[str | None] = mapped_column(String(200), index=True, nullable=True)
    contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    # Set when a worker takes the event. Used to reclaim work abandoned by a crash.
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        # The whole idempotency guarantee rests on this one line.
        UniqueConstraint("idempotency_key", name="uq_webhook_events_idempotency_key"),
        Index("ix_webhook_events_status_received", "status", "received_at"),
    )


class Lead(Base):
    """One person, per GHL location. Survives across events."""

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_key: Mapped[str] = mapped_column(String(200), nullable=False)
    location_id: Mapped[str] = mapped_column(String(64), index=True)

    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254), default="", index=True)
    phone: Mapped[str] = mapped_column(String(40), default="", index=True)

    first_source: Mapped[str] = mapped_column(String(32), default="")
    last_source: Mapped[str] = mapped_column(String(32), default="")
    submission_count: Mapped[int] = mapped_column(Integer, default=0)

    ghl_contact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    open_opportunity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    last_qualification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_routing: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("identity_key", name="uq_leads_identity_key"),)


class AuditEntry(Base):
    """Append-only. Never updated, never deleted by the application."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), ForeignKey("webhook_events.id"), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    step: Mapped[str] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class StepRecord(Base):
    """Memoized result of one pipeline step for one event.

    This is what makes a retry safe after a *partial* failure: if `ghl_contact`
    already succeeded, the retry reads the contact id from here instead of
    calling `POST /contacts/upsert` a second time.
    """

    __tablename__ = "step_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    step_key: Mapped[str] = mapped_column(String(280), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    step: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (UniqueConstraint("step_key", name="uq_step_records_step_key"),)


class DeadLetter(Base):
    """Terminal failures, kept replayable."""

    __tablename__ = "dead_letters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(String(64))
    error: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ProviderCall(Base):
    """Every outbound call to GHL or the AI provider: latency, attempts, outcome.

    Observability that is actually useful during an incident is per-dependency,
    not a single service-level counter.
    """

    __tablename__ = "provider_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    operation: Mapped[str] = mapped_column(String(64))
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
    error_code: Mapped[str] = mapped_column(String(48), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
