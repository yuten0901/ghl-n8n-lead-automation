"""The orchestrator.

One function, `process_lead`, running an ordered list of steps against a single
webhook delivery. What makes it more than a script:

* **Steps are memoized per event.** `_step()` reads `step_records` first. A retry
  after a partial failure resumes at the first incomplete step, so a lead whose
  opportunity write failed does not get a second contact on the next attempt.
* **Failures are classified, not caught.** Retryable failures leave the event
  `failed` and retryable; permanent ones go to the dead-letter table with the
  original payload attached, ready to replay by hand.
* **Everything is recorded.** Each step writes an audit row; each outbound call
  writes a provider-call row with latency and attempt count. When a client asks
  "why did this lead get texted at 2am?", the answer is a query.

The n8n workflow calls this through the webhook API. It is deliberately possible
to run the whole thing without n8n (`scripts/demo.py`), because a pipeline you
can only exercise through a UI is a pipeline you cannot test.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from leadops.ai.providers import build_provider
from leadops.ai.qualify import qualify
from leadops.config import Settings
from leadops.errors import LeadOpsError, ValidationFailed
from leadops.ghl import operations as ops
from leadops.ghl.client import GHLClient
from leadops.ghl.mapping import GHLMapping
from leadops.idempotency import event_idempotency_key, is_weak, lead_identity_key, step_key
from leadops.logging_setup import get_logger
from leadops.models import (
    CanonicalLead,
    EventStatus,
    ProcessingResult,
    Qualification,
    RoutingDecision,
    StepResult,
)
from leadops.normalize import normalize_payload
from leadops.reliability.retry import RetryPolicy
from leadops.routing.rules import RoutingTable, route
from leadops.storage import repo

log = get_logger(__name__)


@dataclass
class PipelineContext:
    """Everything a run needs, injected rather than imported.

    Tests construct this with a fake GHL client and a stub provider; production
    builds it from Settings. There is no global state to reset between runs.
    """

    settings: Settings
    session: AsyncSession
    ghl: GHLClient
    mapping: GHLMapping
    table: RoutingTable
    correlation_id: str
    event_id: str = ""
    idempotency_key: str = ""
    steps: list[StepResult] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.steps is None:
            self.steps = []


def new_correlation_id() -> str:
    return f"cid_{uuid.uuid4().hex[:16]}"


async def _commit(session: AsyncSession) -> None:
    """Close the current write transaction before doing any network I/O.

    This is a correctness requirement, not tidiness. Two reasons:

    * **The idempotency ledger only works if it is visible.** A competing
      delivery has to be able to see our INSERT and lose the UNIQUE race. An
      uncommitted row is invisible, so every concurrent delivery would think it
      was first.
    * **A lock held across an HTTP call is a lock held for as long as the vendor
      is slow.** It converts a GoHighLevel slowdown into database contention for
      every other lead in flight. With a blocking driver it is worse than that -
      the waiting writer stalls the event loop that would have released the lock,
      which is why `storage/db.py` uses an async driver.

    So every step commits before it calls out, and each step is durable on its
    own. The unit of atomicity is deliberately the *step*, not the whole event -
    which is exactly why steps are memoized and resumable.
    """
    await session.commit()


async def _step(
    ctx: PipelineContext,
    name: str,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    *,
    memoize: bool = True,
) -> dict[str, Any]:
    """Run one step, or return the memoized result of a previous successful run.

    `memoize=False` is for steps that are pure and cheap (normalize, qualify with
    the deterministic provider) where re-running costs nothing; the CRM writes are
    the ones that must never happen twice.
    """
    key = step_key(ctx.idempotency_key, name)

    if memoize:
        cached = await repo.read_step(ctx.session, key)
        if cached is not None:
            log.info("step.skipped", extra={"step": name, "correlation_id": ctx.correlation_id})
            ctx.steps.append(StepResult(name=name, ok=True, output=cached, attempts=0))
            return cached

    started = time.perf_counter()
    try:
        output = await operation()
    except LeadOpsError as exc:
        duration = int((time.perf_counter() - started) * 1000)
        ctx.steps.append(
            StepResult(name=name, ok=False, output={}, error=exc.as_dict(), duration_ms=duration)
        )
        await repo.record_audit(
            ctx.session,
            event_id=ctx.event_id,
            correlation_id=ctx.correlation_id,
            step=name,
            ok=False,
            duration_ms=duration,
            detail={"error": exc.as_dict(), "retryable": exc.retryable},
        )
        await _commit(ctx.session)
        log.warning(
            "step.failed",
            extra={
                "step": name,
                "correlation_id": ctx.correlation_id,
                "error_code": exc.code,
                "retryable": exc.retryable,
            },
        )
        raise

    duration = int((time.perf_counter() - started) * 1000)
    if memoize:
        await repo.write_step(
            ctx.session, step_key=key, event_id=ctx.event_id, step=name, output=output
        )
    ctx.steps.append(StepResult(name=name, ok=True, output=output, duration_ms=duration))
    await repo.record_audit(
        ctx.session,
        event_id=ctx.event_id,
        correlation_id=ctx.correlation_id,
        step=name,
        ok=True,
        duration_ms=duration,
        detail={k: v for k, v in output.items() if k not in {"raw", "payload"}},
    )
    await _commit(ctx.session)
    return output


async def process_lead(
    *,
    source: str,
    payload: dict,
    headers: dict[str, str],
    settings: Settings,
    session: AsyncSession,
    ghl: GHLClient | None = None,
    mapping: GHLMapping | None = None,
    table: RoutingTable | None = None,
    provider: Any | None = None,
    explicit_idempotency_key: str | None = None,
    signature_verified: bool = False,
    correlation_id: str | None = None,
) -> ProcessingResult:
    """Process one webhook delivery, all the way to the CRM.

    Returns a result for every outcome including failure; raises only on
    programmer error. The HTTP layer decides the status code from
    `result.status`, so the transport concern stays in the transport layer.
    """
    correlation_id = correlation_id or new_correlation_id()
    mapping = mapping or GHLMapping.load(settings.ghl_mapping_path)
    table = table or RoutingTable.load(settings.routing_config_path)

    idem_key, derivation = event_idempotency_key(
        source=source, payload=payload, headers=headers, explicit_key=explicit_idempotency_key
    )
    if is_weak(derivation):
        # Not an error - but the operator should know which integrations are
        # relying on body hashing rather than a real delivery id.
        log.info(
            "idempotency.weak_key",
            extra={"source": source, "derivation": derivation, "correlation_id": correlation_id},
        )

    claim = await repo.claim_event(
        session,
        idempotency_key=idem_key,
        key_derivation=derivation,
        correlation_id=correlation_id,
        source=source,
        raw_payload=payload,
        signature_verified=signature_verified,
        lease_seconds=settings.processing_lease_seconds,
    )
    # Publish the claim before any network call: see _commit().
    await _commit(session)

    # --- duplicate delivery: do no work, return what we said the first time ---
    if claim.replay or (not claim.is_new and not claim.reclaimed):
        stored = claim.event.stored_response or {}
        log.info(
            "event.duplicate",
            extra={
                "correlation_id": correlation_id,
                "idempotency_key": idem_key,
                "original_event_id": claim.event.id,
                "original_status": claim.event.status,
            },
        )
        await repo.record_audit(
            session,
            event_id=claim.event.id,
            correlation_id=correlation_id,
            step="duplicate_detected",
            ok=True,
            detail={"original_status": claim.event.status, "derivation": derivation},
        )
        await _commit(session)
        return ProcessingResult(
            correlation_id=correlation_id,
            event_id=claim.event.id,
            idempotency_key=idem_key,
            status=EventStatus.DUPLICATE,
            duplicate_of=claim.event.id,
            contact_id=claim.event.contact_id,
            opportunity_id=claim.event.opportunity_id,
            qualification=(
                Qualification.model_validate(stored["qualification"])
                if stored.get("qualification")
                else None
            ),
            routing=(
                RoutingDecision.model_validate(stored["routing"]) if stored.get("routing") else None
            ),
        )

    event = claim.event
    ghl = ghl or GHLClient(
        base_url=settings.ghl_base_url,
        access_token=settings.ghl_access_token,
        location_id=settings.ghl_location_id,
        api_version=settings.ghl_api_version,
        timeout_seconds=settings.ghl_timeout_seconds,
        policy=RetryPolicy(
            max_attempts=settings.ghl_max_attempts,
            base_seconds=settings.ghl_backoff_base_seconds,
            max_seconds=settings.ghl_backoff_max_seconds,
        ),
        correlation_id=correlation_id,
    )
    ghl.correlation_id = correlation_id

    ctx = PipelineContext(
        settings=settings,
        session=session,
        ghl=ghl,
        mapping=mapping,
        table=table,
        correlation_id=correlation_id,
        event_id=event.id,
        idempotency_key=idem_key,
    )

    lead: CanonicalLead | None = None
    qualification: Qualification | None = None
    routing: RoutingDecision | None = None
    contact_id = ""
    opportunity_id = ""

    try:
        # --- 1. normalize ------------------------------------------------
        async def _normalize() -> dict:
            return normalize_payload(source, payload).model_dump(mode="json")

        lead = CanonicalLead.model_validate(
            await _step(ctx, "normalize", _normalize, memoize=False)
        )
        identity = lead_identity_key(lead, location_id=mapping.location_id)

        # --- 2. lead identity (the *person*, across events) ---------------
        async def _identity() -> dict:
            stored_lead, is_new = await repo.upsert_lead(
                session,
                identity_key=identity,
                location_id=mapping.location_id,
                first_name=lead.first_name,
                last_name=lead.last_name,
                email=lead.email,
                phone=lead.phone,
                source=lead.source.value,
            )
            return {
                "lead_id": stored_lead.id,
                "identity_key": identity,
                "is_new_person": is_new,
                "submission_count": stored_lead.submission_count,
                "known_contact_id": stored_lead.ghl_contact_id or "",
            }

        identity_info = await _step(ctx, "lead_identity", _identity, memoize=False)

        # --- 3. AI qualification -----------------------------------------
        model = provider or build_provider(
            provider=settings.ai_provider,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            timeout_seconds=settings.ai_timeout_seconds,
            temperature=settings.ai_temperature,
        )

        async def _qualify() -> dict:
            outcome = await qualify(
                lead, provider=model, table=table, max_attempts=settings.ai_max_attempts
            )
            await repo.record_provider_call(
                session,
                event_id=event.id,
                correlation_id=correlation_id,
                provider=outcome.provider_used,
                operation="qualify",
                ok=not outcome.qualification.degraded,
                attempts=max(1, outcome.attempts),
                duration_ms=outcome.duration_ms,
                error_code="degraded" if outcome.qualification.degraded else "",
            )
            return {
                "qualification": outcome.qualification.model_dump(mode="json"),
                "provider_used": outcome.provider_used,
                "notes": outcome.notes,
            }

        qualify_out = await _step(ctx, "ai_qualification", _qualify, memoize=True)
        qualification = Qualification.model_validate(qualify_out["qualification"])

        # --- 4. routing (deterministic) ----------------------------------
        async def _route() -> dict:
            decision = route(qualification, table)
            if not mapping.has_stage(decision.pipeline_stage):
                # Config drift: a rule names a stage the mapping does not have.
                # Land the lead in new_lead and make the drift loud.
                log.warning(
                    "routing.unknown_stage",
                    extra={
                        "stage": decision.pipeline_stage,
                        "rule_id": decision.rule_id,
                        "correlation_id": correlation_id,
                    },
                )
            return decision.model_dump(mode="json")

        routing = RoutingDecision.model_validate(await _step(ctx, "routing", _route, memoize=False))

        # --- 5. CRM contact (first side effect) --------------------------
        async def _contact() -> dict:
            result = await ops.upsert_contact(
                ghl, lead, qualification, routing, mapping, correlation_id=correlation_id
            )
            await repo.record_provider_call(
                session,
                event_id=event.id,
                correlation_id=correlation_id,
                provider="ghl",
                operation="contacts.upsert",
                ok=bool(result.contact_id),
            )
            if not result.contact_id:
                raise ValidationFailed(
                    "GoHighLevel returned no contact id",
                    detail={"body_keys": sorted(result.raw)[:10]},
                )
            return {"contact_id": result.contact_id, "created": result.created}

        contact = await _step(ctx, "ghl_contact", _contact)
        contact_id = contact["contact_id"]

        stored_lead = await repo.get_lead(session, identity)
        if stored_lead is not None:
            stored_lead.ghl_contact_id = contact_id
            stored_lead.last_qualification = qualification.model_dump(mode="json")
            stored_lead.last_routing = routing.model_dump(mode="json")

        # --- 6. summary note ---------------------------------------------
        async def _note() -> dict:
            response = await ops.attach_summary_note(ghl, contact_id, lead, qualification)
            return {"note_status": response.status_code}

        await _step(ctx, "ghl_note", _note)

        # --- 7. opportunity -----------------------------------------------
        async def _opportunity() -> dict:
            opp_id, created = await ops.ensure_opportunity(
                ghl,
                contact_id=contact_id,
                lead=lead,
                qualification=qualification,
                routing=routing,
                mapping=mapping,
            )
            await repo.record_provider_call(
                session,
                event_id=event.id,
                correlation_id=correlation_id,
                provider="ghl",
                operation="opportunities.ensure",
                ok=bool(opp_id),
            )
            return {"opportunity_id": opp_id, "created": created}

        opportunity = await _step(ctx, "ghl_opportunity", _opportunity)
        opportunity_id = opportunity["opportunity_id"]
        if stored_lead is not None:
            stored_lead.open_opportunity_id = opportunity_id

        # --- 8. follow-up (reaches the customer) --------------------------
        async def _follow_up() -> dict:
            response = await ops.send_follow_up(
                ghl,
                contact_id=contact_id,
                lead=lead,
                qualification=qualification,
                routing=routing,
            )
            return {
                "sent": response is not None,
                "sequence": routing.follow_up_sequence,
                "status": response.status_code if response else None,
            }

        await _step(ctx, "follow_up", _follow_up)

        # --- 9. internal notification -------------------------------------
        async def _notify() -> dict:
            response = await ops.notify_sales(
                ghl,
                contact_id=contact_id,
                lead=lead,
                qualification=qualification,
                routing=routing,
                mapping=mapping,
                notification_email=settings.sales_notification_email,
            )
            return {"notified": response is not None, "channel": routing.notify_channel}

        await _step(ctx, "notify_sales", _notify)

    except LeadOpsError as exc:
        return await _fail(
            ctx,
            event=event,
            exc=exc,
            payload=payload,
            source=source,
            lead=lead,
            qualification=qualification,
            routing=routing,
            contact_id=contact_id,
            opportunity_id=opportunity_id,
        )

    result = ProcessingResult(
        correlation_id=correlation_id,
        event_id=event.id,
        idempotency_key=idem_key,
        status=EventStatus.SUCCEEDED,
        lead=lead,
        qualification=qualification,
        routing=routing,
        contact_id=contact_id,
        opportunity_id=opportunity_id,
        steps=ctx.steps,
    )
    await repo.finish_event(
        session,
        event,
        status=EventStatus.SUCCEEDED,
        stored_response=result.public_response(),
        lead_identity_key=identity,
        contact_id=contact_id,
        opportunity_id=opportunity_id,
    )
    await _commit(session)
    log.info(
        "event.succeeded",
        extra={
            "correlation_id": correlation_id,
            "event_id": event.id,
            "rule_id": routing.rule_id if routing else "",
            "score": qualification.qualification_score if qualification else None,
            "degraded": qualification.degraded if qualification else None,
            "submission_count": identity_info.get("submission_count"),
        },
    )
    return result


async def _fail(
    ctx: PipelineContext,
    *,
    event: Any,
    exc: LeadOpsError,
    payload: dict,
    source: str,
    lead: CanonicalLead | None,
    qualification: Qualification | None,
    routing: RoutingDecision | None,
    contact_id: str,
    opportunity_id: str,
) -> ProcessingResult:
    """Decide between "retry this later" and "a human has to look at it".

    A permanent error, or a retryable one that has burned its delivery budget,
    becomes a dead letter with the original payload attached. A retryable error
    inside budget leaves the event `failed`, which the reclaim path in
    `claim_event` picks up on the next delivery.
    """
    exhausted = event.attempts >= ctx.settings.max_delivery_attempts
    terminal = (not exc.retryable) or exhausted

    if terminal:
        await repo.dead_letter(
            ctx.session,
            event_id=event.id,
            correlation_id=ctx.correlation_id,
            source=source,
            idempotency_key=ctx.idempotency_key,
            reason="permanent" if not exc.retryable else "attempts_exhausted",
            error=exc.as_dict(),
            raw_payload=payload,
            attempts=event.attempts,
        )
        status = EventStatus.DEAD_LETTERED
    else:
        status = EventStatus.FAILED

    log.error(
        "event.failed",
        extra={
            "correlation_id": ctx.correlation_id,
            "event_id": event.id,
            "error_code": exc.code,
            "retryable": exc.retryable,
            "attempts": event.attempts,
            "terminal": terminal,
            "completed_steps": [s.name for s in ctx.steps if s.ok],
        },
    )

    result = ProcessingResult(
        correlation_id=ctx.correlation_id,
        event_id=event.id,
        idempotency_key=ctx.idempotency_key,
        status=status,
        lead=lead,
        qualification=qualification,
        routing=routing,
        contact_id=contact_id or None,
        opportunity_id=opportunity_id or None,
        steps=ctx.steps,
        error=exc.as_dict() | {"terminal": terminal, "attempts": event.attempts},
    )
    await repo.finish_event(
        ctx.session,
        event,
        status=status,
        error=result.error,
        contact_id=contact_id or None,
        opportunity_id=opportunity_id or None,
    )
    await _commit(ctx.session)
    return result
