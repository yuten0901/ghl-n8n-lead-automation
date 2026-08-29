"""HTTP surface.

Two groups of routes:

* `/webhooks/leads/{source}` - what n8n (or a form, or Meta) posts to.
* `/admin/*`                 - read-only operational views plus a replay endpoint.
                               These are what turn "the automation is broken" into
                               a diagnosis, and they are the reason this system is
                               debuggable without opening the n8n UI.

Status-code policy is deliberate and documented in `docs/architecture.md`:

    200  processed, or a duplicate we already processed
    202  accepted but failed retryably - the sender SHOULD redeliver
    400  the payload itself is wrong - redelivering will never help
    401  bad signature
    422  dead-lettered on our side - a human must look, do not redeliver

Returning 200 for everything is the most common webhook mistake: the sender stops
retrying exactly when retrying is what you need. The 400/422 split matters too -
both say "stop", but 400 says "your payload", 422 says "our problem", and that is
the difference between the client fixing their form and the client calling us.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse

from leadops import __version__
from leadops.api.security import verify
from leadops.config import Settings, get_settings
from leadops.errors import SignatureInvalid
from leadops.ghl.mapping import GHLMapping
from leadops.logging_setup import configure_logging, get_logger
from leadops.models import EventStatus
from leadops.normalize.adapters import SOURCE_ADAPTERS
from leadops.pipeline import new_correlation_id, process_lead
from leadops.routing.rules import RoutingTable
from leadops.storage import create_all, init_engine, repo, session_scope
from leadops.storage.schema import utcnow

log = get_logger(__name__)

STATUS_CODES = {
    EventStatus.SUCCEEDED: 200,
    EventStatus.DUPLICATE: 200,
    EventStatus.FAILED: 202,
    EventStatus.DEAD_LETTERED: 422,
}

# Terminal errors that are the *sender's* fault rather than ours. These are still
# dead-lettered - someone should see that a form is posting unusable leads - but
# the caller is told 400, because no redelivery of that body can ever succeed.
CLIENT_ERROR_CODES = frozenset({"validation_failed", "signature_invalid"})


def status_code_for(result) -> int:  # noqa: ANN001
    code = STATUS_CODES.get(result.status, 200)
    if code == 422 and (result.error or {}).get("code") in CLIENT_ERROR_CODES:
        return 400
    return code


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    init_engine(settings.database_url)
    await create_all()
    # Loaded once at startup: a config typo should fail the deploy, not the
    # first lead that arrives at 6pm on a Friday.
    app.state.mapping = GHLMapping.load(settings.ghl_mapping_path)
    app.state.table = RoutingTable.load(settings.routing_config_path)
    log.info("service.started", extra={"version": __version__, **settings.redacted()})
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="Lead Operations Automation",
        version=__version__,
        description="Webhook ingestion, idempotent GoHighLevel sync, AI lead qualification.",
        lifespan=lifespan,
    )
    if settings is not None:
        app.state.settings_override = settings

    def current_settings() -> Settings:
        return getattr(app.state, "settings_override", None) or get_settings()

    # ---- health ---------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Reports what is *configured*, not what is reachable.

        Deliberate: a health check that calls GHL turns their outage into our
        outage and gets the service restarted by an orchestrator for no reason.
        Dependency health belongs in /admin/stats, which is read on purpose.
        """
        s = current_settings()
        return {
            "status": "ok",
            "version": __version__,
            "sources": sorted(SOURCE_ADAPTERS),
            "config": s.redacted(),
        }

    # ---- webhook --------------------------------------------------------

    @app.post("/webhooks/leads/{source}")
    async def ingest(
        source: str,
        request: Request,
        x_signature: str | None = Header(default=None, alias="X-Signature"),
        x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    ) -> Response:
        settings = current_settings()
        correlation_id = new_correlation_id()

        # Raw bytes: the signature covers what was sent, not what we re-encoded.
        raw = await request.body()

        try:
            check = verify(
                raw,
                x_signature,
                settings.webhook_signing_secret,
                max_skew_seconds=settings.webhook_max_skew_seconds,
                required=settings.require_signature,
            )
        except SignatureInvalid as exc:
            log.warning(
                "webhook.signature_rejected",
                extra={
                    "source": source,
                    "correlation_id": correlation_id,
                    "reason": exc.message,
                },
            )
            return JSONResponse(
                status_code=401,
                content={
                    "status": "rejected",
                    "error": exc.as_dict(),
                    "correlation_id": correlation_id,
                },
            )

        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "error": {"code": "malformed_json", "message": str(exc)[:200]},
                    "correlation_id": correlation_id,
                },
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400,
                content={
                    "status": "rejected",
                    "error": {"code": "malformed_json", "message": "body must be a JSON object"},
                    "correlation_id": correlation_id,
                },
            )

        headers = {k.lower(): v for k, v in request.headers.items()}

        async with session_scope() as session:
            result = await process_lead(
                source=source,
                payload=payload,
                headers=headers,
                settings=settings,
                session=session,
                mapping=getattr(app.state, "mapping", None),
                table=getattr(app.state, "table", None),
                explicit_idempotency_key=x_idempotency_key,
                signature_verified=check.verified,
                correlation_id=correlation_id,
            )
            body = result.public_response()
            status_code = status_code_for(result)

        body["signature_verified"] = check.verified
        return JSONResponse(status_code=status_code, content=body)

    # ---- admin ----------------------------------------------------------

    @app.get("/admin/events")
    async def list_events(status: str | None = None, limit: int = 50) -> dict[str, Any]:
        async with session_scope() as session:
            events = await repo.list_events(session, status=status, limit=min(limit, 200))
            return {
                "count": len(events),
                "events": [
                    {
                        "event_id": e.id,
                        "correlation_id": e.correlation_id,
                        "source": e.source,
                        "status": e.status,
                        "attempts": e.attempts,
                        "idempotency_key": e.idempotency_key,
                        "key_derivation": e.key_derivation,
                        "signature_verified": e.signature_verified,
                        "contact_id": e.contact_id,
                        "opportunity_id": e.opportunity_id,
                        "received_at": e.received_at.isoformat(),
                        "error": e.error,
                    }
                    for e in events
                ],
            }

    @app.get("/admin/events/{event_id}")
    async def event_detail(event_id: str) -> Response:
        """The whole story of one lead: steps, timings, outbound calls, errors.

        This endpoint is what makes 'workflow troubleshooting' a five-minute job
        instead of scrolling n8n executions.
        """
        async with session_scope() as session:
            event = await repo.get_event(session, event_id)
            if event is None:
                return JSONResponse(status_code=404, content={"error": "event not found"})
            audit = await repo.audit_for_event(session, event_id)
            calls = await repo.provider_calls_for_event(session, event_id)
            return JSONResponse(
                content={
                    "event": {
                        "event_id": event.id,
                        "correlation_id": event.correlation_id,
                        "source": event.source,
                        "status": event.status,
                        "attempts": event.attempts,
                        "idempotency_key": event.idempotency_key,
                        "key_derivation": event.key_derivation,
                        "signature_verified": event.signature_verified,
                        "error": event.error,
                        "received_at": event.received_at.isoformat(),
                    },
                    "result": event.stored_response,
                    "steps": [
                        {
                            "step": a.step,
                            "ok": a.ok,
                            "duration_ms": a.duration_ms,
                            "detail": a.detail,
                            "at": a.created_at.isoformat(),
                        }
                        for a in audit
                    ],
                    "provider_calls": [
                        {
                            "provider": c.provider,
                            "operation": c.operation,
                            "ok": c.ok,
                            "attempts": c.attempts,
                            "duration_ms": round(c.duration_ms, 1),
                            "error_code": c.error_code,
                        }
                        for c in calls
                    ],
                }
            )

    @app.get("/admin/dead-letters")
    async def dead_letters(unresolved_only: bool = True, limit: int = 50) -> dict[str, Any]:
        async with session_scope() as session:
            entries = await repo.list_dead_letters(
                session, unresolved_only=unresolved_only, limit=min(limit, 200)
            )
            return {
                "count": len(entries),
                "dead_letters": [
                    {
                        "id": d.id,
                        "event_id": d.event_id,
                        "correlation_id": d.correlation_id,
                        "source": d.source,
                        "reason": d.reason,
                        "attempts": d.attempts,
                        "error": d.error,
                        "resolved": d.resolved,
                        "created_at": d.created_at.isoformat(),
                    }
                    for d in entries
                ],
            }

    @app.post("/admin/dead-letters/{dead_letter_id}/replay")
    async def replay(dead_letter_id: int) -> Response:
        """Re-run a dead-lettered event from its stored payload.

        Reuses the original idempotency key on purpose: completed steps are still
        memoized, so a replay after fixing a GHL credential resumes at the failed
        step rather than creating a second contact.
        """
        settings = current_settings()
        async with session_scope() as session:
            entries = await repo.list_dead_letters(session, unresolved_only=False, limit=200)
            entry = next((d for d in entries if d.id == dead_letter_id), None)
            if entry is None:
                return JSONResponse(status_code=404, content={"error": "dead letter not found"})
            payload, source, key = entry.raw_payload, entry.source, entry.idempotency_key
            original_event = await repo.get_event(session, entry.event_id)
            if original_event is not None:
                # Free the key so claim_event() can reclaim rather than replay.
                original_event.status = EventStatus.FAILED.value

        async with session_scope() as session:
            result = await process_lead(
                source=source,
                payload=payload,
                headers={},
                settings=settings,
                session=session,
                mapping=getattr(app.state, "mapping", None),
                table=getattr(app.state, "table", None),
                explicit_idempotency_key=key.split(":", 1)[1] if ":" in key else key,
                signature_verified=False,
            )
            if result.status is EventStatus.SUCCEEDED:
                await repo.resolve_dead_letter(session, dead_letter_id, "replayed successfully")
            body = result.public_response()

        return JSONResponse(status_code=status_code_for(result), content=body)

    @app.get("/admin/stats")
    async def stats() -> dict[str, Any]:
        """Small enough to read at a glance during an incident."""
        async with session_scope() as session:
            events = await repo.list_events(session, limit=200)
            by_status: dict[str, int] = {}
            weak_keys = 0
            unverified = 0
            for e in events:
                by_status[e.status] = by_status.get(e.status, 0) + 1
                if e.key_derivation == "body_hash":
                    weak_keys += 1
                if not e.signature_verified:
                    unverified += 1
            open_dead_letters = len(
                await repo.list_dead_letters(session, unresolved_only=True, limit=200)
            )
            # Events whose worker took a lease and never came back. Nothing
            # sweeps these today - `claim_event` only reclaims one if a
            # redelivery happens to arrive - so counting them is the difference
            # between a lead that vanished and a lead you can see has vanished.
            # docs/limitations.md lists the sweeper as a go-live item.
            now = utcnow()
            stale_processing = sum(
                1
                for e in events
                if e.status == EventStatus.PROCESSING.value
                and e.lease_expires_at is not None
                and e.lease_expires_at <= now
            )
            return {
                "sampled_events": len(events),
                "by_status": by_status,
                "weak_idempotency_keys": weak_keys,
                "unverified_signatures": unverified,
                "open_dead_letters": open_dead_letters,
                "stale_processing": stale_processing,
            }

    return app


app = create_app()
