"""Repository layer: every database access the pipeline makes.

The important function here is `claim_event`. It implements insert-first
idempotency:

    INSERT the event row  -> we own it, process it
    IntegrityError        -> someone else owns it, return their result

There is no SELECT-then-INSERT anywhere, because that pattern loses the race it
is supposed to win. Everything else in this file is bookkeeping around that.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from leadops.models import EventStatus
from leadops.storage.schema import (
    AuditEntry,
    DeadLetter,
    Lead,
    ProviderCall,
    StepRecord,
    WebhookEvent,
    utcnow,
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _forget(session: AsyncSession, instance: object) -> None:
    """Detach an instance that lost an INSERT race, tolerating the case where the
    savepoint rollback already removed it."""
    with contextlib.suppress(InvalidRequestError):
        session.expunge(instance)


@dataclass(slots=True)
class ClaimOutcome:
    """Result of trying to take ownership of a webhook delivery."""

    event: WebhookEvent
    is_new: bool
    # True when a previous delivery already finished and we should replay it.
    replay: bool
    # True when a previous attempt was abandoned (crashed worker) and we reclaimed it.
    reclaimed: bool


async def claim_event(
    session: AsyncSession,
    *,
    idempotency_key: str,
    key_derivation: str,
    correlation_id: str,
    source: str,
    raw_payload: dict,
    signature_verified: bool,
    lease_seconds: int,
) -> ClaimOutcome:
    """Take ownership of this delivery, or discover that someone already has.

    Four cases, all of which happen in production:

    * brand new                  -> we insert and own it
    * already succeeded          -> replay the stored response, do no work
    * currently being processed  -> report duplicate, do no work
    * previously failed / stale  -> reclaim and retry, reusing memoized steps
    """
    event = WebhookEvent(
        id=new_id("evt"),
        idempotency_key=idempotency_key,
        key_derivation=key_derivation,
        correlation_id=correlation_id,
        source=source,
        status=EventStatus.PROCESSING.value,
        signature_verified=signature_verified,
        attempts=1,
        raw_payload=raw_payload,
        lease_expires_at=utcnow() + timedelta(seconds=lease_seconds),
    )
    try:
        # SAVEPOINT opened *before* the add: begin_nested() flushes pending
        # objects as it starts, so an add() outside it would fail outside it too,
        # poisoning the outer transaction instead of being caught here.
        async with session.begin_nested():
            session.add(event)
            await session.flush()
        return ClaimOutcome(event=event, is_new=True, replay=False, reclaimed=False)
    except IntegrityError:
        # The savepoint rollback already detached the losing row.
        _forget(session, event)

    existing = (
        await session.execute(
            select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one()

    if existing.status in (EventStatus.SUCCEEDED.value, EventStatus.DUPLICATE.value):
        return ClaimOutcome(event=existing, is_new=False, replay=True, reclaimed=False)

    lease_expired = existing.lease_expires_at is None or existing.lease_expires_at <= utcnow()
    if existing.status == EventStatus.PROCESSING.value and not lease_expired:
        # Another worker holds a live lease. Do not touch its work.
        return ClaimOutcome(event=existing, is_new=False, replay=False, reclaimed=False)

    # Failed, dead-lettered, or abandoned mid-flight: safe to take over, because
    # completed steps are memoized in step_records and will be skipped.
    existing.status = EventStatus.PROCESSING.value
    existing.attempts += 1
    existing.correlation_id = correlation_id
    existing.lease_expires_at = utcnow() + timedelta(seconds=lease_seconds)
    await session.flush()
    return ClaimOutcome(event=existing, is_new=False, replay=False, reclaimed=True)


async def finish_event(
    session: AsyncSession,
    event: WebhookEvent,
    *,
    status: EventStatus,
    stored_response: dict | None = None,
    error: dict | None = None,
    lead_identity_key: str | None = None,
    contact_id: str | None = None,
    opportunity_id: str | None = None,
) -> None:
    event.status = status.value
    event.lease_expires_at = None
    if stored_response is not None:
        event.stored_response = stored_response
    event.error = error
    if lead_identity_key:
        event.lead_identity_key = lead_identity_key
    if contact_id:
        event.contact_id = contact_id
    if opportunity_id:
        event.opportunity_id = opportunity_id
    await session.flush()


async def get_event(session: AsyncSession, event_id: str) -> WebhookEvent | None:
    return await session.get(WebhookEvent, event_id)


async def get_event_by_key(session: AsyncSession, idempotency_key: str) -> WebhookEvent | None:
    return (
        await session.execute(
            select(WebhookEvent).where(WebhookEvent.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()


# --- memoized steps -------------------------------------------------------


async def read_step(session: AsyncSession, step_key: str) -> dict | None:
    record = (
        await session.execute(select(StepRecord).where(StepRecord.step_key == step_key))
    ).scalar_one_or_none()
    return record.output if record else None


async def write_step(
    session: AsyncSession, *, step_key: str, event_id: str, step: str, output: dict
) -> None:
    """Record a completed step. Idempotent: a concurrent duplicate write is a
    no-op rather than an error, because both writers computed the same thing."""
    record = StepRecord(step_key=step_key, event_id=event_id, step=step, output=output)
    try:
        async with session.begin_nested():
            session.add(record)
            await session.flush()
    except IntegrityError:
        _forget(session, record)


# --- leads ----------------------------------------------------------------


async def upsert_lead(
    session: AsyncSession,
    *,
    identity_key: str,
    location_id: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    source: str,
) -> tuple[Lead, bool]:
    """Return (lead, is_new). Never creates a second row for the same person."""
    existing = (
        await session.execute(select(Lead).where(Lead.identity_key == identity_key))
    ).scalar_one_or_none()

    if existing is not None:
        # A returning lead: keep the strongest known values rather than letting a
        # sparser later submission blank out fields we already had.
        existing.first_name = first_name or existing.first_name
        existing.last_name = last_name or existing.last_name
        existing.email = email or existing.email
        existing.phone = phone or existing.phone
        existing.last_source = source
        existing.submission_count += 1
        await session.flush()
        return existing, False

    lead = Lead(
        id=new_id("lead"),
        identity_key=identity_key,
        location_id=location_id,
        first_name=first_name,
        last_name=last_name,
        email=email,
        phone=phone,
        first_source=source,
        last_source=source,
        submission_count=1,
    )
    try:
        async with session.begin_nested():
            session.add(lead)
            await session.flush()
        return lead, True
    except IntegrityError:
        _forget(session, lead)
        found = (
            await session.execute(select(Lead).where(Lead.identity_key == identity_key))
        ).scalar_one()
        found.submission_count += 1
        found.last_source = source
        await session.flush()
        return found, False


async def get_lead(session: AsyncSession, identity_key: str) -> Lead | None:
    return (
        await session.execute(select(Lead).where(Lead.identity_key == identity_key))
    ).scalar_one_or_none()


# --- observability --------------------------------------------------------


async def record_audit(
    session: AsyncSession,
    *,
    event_id: str,
    correlation_id: str,
    step: str,
    ok: bool,
    attempts: int = 1,
    duration_ms: int = 0,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEntry(
            event_id=event_id,
            correlation_id=correlation_id,
            step=step,
            ok=ok,
            attempts=attempts,
            duration_ms=duration_ms,
            detail=detail or {},
        )
    )
    await session.flush()


async def record_provider_call(
    session: AsyncSession,
    *,
    event_id: str,
    correlation_id: str,
    provider: str,
    operation: str,
    ok: bool,
    status_code: int | None = None,
    attempts: int = 1,
    duration_ms: float = 0.0,
    error_code: str = "",
) -> None:
    session.add(
        ProviderCall(
            event_id=event_id,
            correlation_id=correlation_id,
            provider=provider,
            operation=operation,
            ok=ok,
            status_code=status_code,
            attempts=attempts,
            duration_ms=duration_ms,
            error_code=error_code,
        )
    )
    await session.flush()


async def audit_for_event(session: AsyncSession, event_id: str) -> list[AuditEntry]:
    return list(
        (
            await session.execute(
                select(AuditEntry).where(AuditEntry.event_id == event_id).order_by(AuditEntry.id)
            )
        ).scalars()
    )


async def provider_calls_for_event(session: AsyncSession, event_id: str) -> list[ProviderCall]:
    return list(
        (
            await session.execute(
                select(ProviderCall)
                .where(ProviderCall.event_id == event_id)
                .order_by(ProviderCall.id)
            )
        ).scalars()
    )


# --- dead letters ---------------------------------------------------------


async def dead_letter(
    session: AsyncSession,
    *,
    event_id: str,
    correlation_id: str,
    source: str,
    idempotency_key: str,
    reason: str,
    error: dict,
    raw_payload: dict,
    attempts: int,
) -> DeadLetter:
    entry = DeadLetter(
        event_id=event_id,
        correlation_id=correlation_id,
        source=source,
        idempotency_key=idempotency_key,
        reason=reason,
        error=error,
        raw_payload=raw_payload,
        attempts=attempts,
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_dead_letters(
    session: AsyncSession, *, unresolved_only: bool = True, limit: int = 100
) -> list[DeadLetter]:
    stmt = select(DeadLetter).order_by(DeadLetter.id.desc()).limit(limit)
    if unresolved_only:
        stmt = stmt.where(DeadLetter.resolved.is_(False))
    return list((await session.execute(stmt)).scalars())


async def resolve_dead_letter(
    session: AsyncSession, dead_letter_id: int, note: str
) -> DeadLetter | None:
    entry = await session.get(DeadLetter, dead_letter_id)
    if entry is None:
        return None
    entry.resolved = True
    entry.resolved_note = note[:2000]
    await session.flush()
    return entry


async def list_events(
    session: AsyncSession, *, status: str | None = None, limit: int = 50
) -> list[WebhookEvent]:
    stmt = select(WebhookEvent).order_by(WebhookEvent.received_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(WebhookEvent.status == status)
    return list((await session.execute(stmt)).scalars())
