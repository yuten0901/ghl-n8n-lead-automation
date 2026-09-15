"""A local stand-in for the current documented GoHighLevel API contract.

What it is: an in-memory implementation of the endpoints this project calls,
returning the documented response shapes, plus a fault-injection layer.

What it is not: a claim that the real API behaves identically in every detail.
It encodes the documented contract in `docs/ghl-integration.md`, and that
document lists what has to be re-verified against a real sub-account.

Why it exists: a reviewer can run the full demo - including retries, rate
limiting, duplicate handling and partial failure - without a paid GHL account,
and CI can assert on those paths deterministically. Faults are *scripted*, not
random, so a test that passes today passes tomorrow.

Fault injection:

    POST /_mock/faults  {"operation": "contacts.upsert", "mode": "500", "times": 2}

Modes: 500, 503, 429, timeout, 401, 422, empty_body.
`GET /_mock/state` returns everything created so far, which is what the
end-to-end test asserts against.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

API_VERSION = "v3"


@dataclass
class Fault:
    mode: str
    remaining: int


@dataclass
class MockState:
    contacts: dict[str, dict] = field(default_factory=dict)
    opportunities: dict[str, dict] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    appointments: list[dict] = field(default_factory=list)
    tags_added: list[dict] = field(default_factory=list)
    call_log: list[dict] = field(default_factory=list)
    faults: dict[str, Fault] = field(default_factory=dict)
    # Counts calls per operation so tests can assert "upsert was called once,
    # even though the webhook was delivered three times".
    call_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def reset(self) -> None:
        self.contacts.clear()
        self.opportunities.clear()
        self.notes.clear()
        self.messages.clear()
        self.appointments.clear()
        self.tags_added.clear()
        self.call_log.clear()
        self.faults.clear()
        self.call_counts.clear()


state = MockState()


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


def _auth_error(request: Request) -> JSONResponse | None:
    """The real API requires both headers. Enforcing them here means a missing
    `Version` header is caught in the demo rather than on the client's account."""
    if request.url.path.startswith("/_mock"):
        return None
    if request.headers.get("Version") != API_VERSION:
        return JSONResponse(
            status_code=400,
            content={"message": f"Version header must be {API_VERSION}", "statusCode": 400},
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or len(auth) < 12:
        return JSONResponse(
            status_code=401,
            content={"message": "Invalid or missing access token", "statusCode": 401},
        )
    return None


async def _apply_fault(operation: str) -> JSONResponse | None:
    """Consume one scheduled fault for this operation, if any."""
    state.call_counts[operation] += 1
    fault = state.faults.get(operation)
    if fault is None or fault.remaining <= 0:
        return None
    fault.remaining -= 1
    if fault.remaining <= 0:
        state.faults.pop(operation, None)

    if fault.mode == "timeout":
        # Longer than any client timeout in this project, so the client's own
        # timeout path is what gets exercised.
        await asyncio.sleep(30)
        return JSONResponse(status_code=504, content={"message": "gateway timeout"})
    if fault.mode == "429":
        return JSONResponse(
            status_code=429,
            content={"message": "Rate limit exceeded", "statusCode": 429},
            headers={"Retry-After": "1"},
        )
    if fault.mode == "empty_body":
        return JSONResponse(status_code=200, content={})
    if fault.mode in {"500", "503", "401", "422"}:
        code = int(fault.mode)
        return JSONResponse(
            status_code=code, content={"message": f"injected fault {code}", "statusCode": code}
        )
    return None


def create_app() -> FastAPI:
    app = FastAPI(title="GoHighLevel v3 contract (mock)", version="1.1.0")

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001
        error = _auth_error(request)
        if error is not None:
            return error
        response = await call_next(request)
        state.call_log.append(
            {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "correlation_id": request.headers.get("X-Correlation-Id", ""),
            }
        )
        return response

    # ---- control plane --------------------------------------------------

    @app.post("/_mock/faults")
    async def set_fault(request: Request) -> dict:
        body = await request.json()
        operation = body["operation"]
        state.faults[operation] = Fault(mode=str(body["mode"]), remaining=int(body.get("times", 1)))
        return {
            "ok": True,
            "operation": operation,
            "fault": body["mode"],
            "times": body.get("times", 1),
        }

    @app.post("/_mock/reset")
    async def reset() -> dict:
        state.reset()
        return {"ok": True}

    @app.get("/_mock/state")
    async def get_state() -> dict:
        return {
            "contacts": list(state.contacts.values()),
            "opportunities": list(state.opportunities.values()),
            "notes": state.notes,
            "messages": state.messages,
            "appointments": state.appointments,
            "tags_added": state.tags_added,
            "call_counts": dict(state.call_counts),
            "call_log": state.call_log[-50:],
        }

    # ---- contacts -------------------------------------------------------

    @app.get("/locations/{location_id}/customFields")
    async def get_custom_fields(location_id: str) -> dict:
        return {
            "customFields": [
                {
                    "id": "cf_TEST_lead_score_000",
                    "name": "Lead Score",
                    "fieldKey": "contact.lead_score",
                    "locationId": location_id,
                    "model": "contact",
                }
            ]
        }

    @app.get("/opportunities/pipelines")
    async def get_pipelines(request: Request) -> dict:
        return {
            "pipelines": [
                {
                    "id": "pipe_TEST00000000000000",
                    "name": "LeadOps Sandbox",
                    "locationId": request.query_params.get("locationId"),
                    "stages": [{"id": "stage_TEST_new_lead_0000", "name": "New Lead"}],
                }
            ]
        }

    @app.get("/calendars/")
    async def get_calendars(request: Request) -> dict:
        return {
            "calendars": [
                {
                    "id": "cal_TEST000000000000000",
                    "locationId": request.query_params.get("locationId"),
                }
            ]
        }

    @app.get("/users/")
    async def get_users(request: Request) -> dict:
        return {
            "users": [
                {
                    "id": "usr_TEST000000000000000",
                    "locationId": request.query_params.get("locationId"),
                }
            ]
        }

    @app.post("/contacts/upsert")
    async def upsert_contact(request: Request) -> JSONResponse:
        fault = await _apply_fault("contacts.upsert")
        if fault is not None:
            return fault

        body: dict[str, Any] = await request.json()
        location_id = body.get("locationId", "")
        email = (body.get("email") or "").lower()
        phone = body.get("phone") or ""

        # The matching rule the real API documents: email first, then phone,
        # scoped to the location. Reproduced here because the whole
        # duplicate-contact story depends on it.
        match = None
        for contact in state.contacts.values():
            if contact.get("locationId") != location_id:
                continue
            if email and contact.get("email", "").lower() == email:
                match = contact
                break
            if phone and contact.get("phone") == phone:
                match = contact
                break

        if match is not None:
            match.update({k: v for k, v in body.items() if v not in (None, "")})
            return JSONResponse(
                status_code=200, content={"succeded": True, "new": False, "contact": match}
            )

        contact_id = _new_id("ct_")
        contact = {
            "id": contact_id,
            "locationId": location_id,
            "dateAdded": "2026-08-29T00:00:00.000Z",
            **{k: v for k, v in body.items() if k != "locationId"},
        }
        contact.setdefault("tags", [])
        state.contacts[contact_id] = contact
        return JSONResponse(
            status_code=201, content={"succeded": True, "new": True, "contact": contact}
        )

    @app.get("/contacts/search/duplicate")
    async def search_duplicate(request: Request) -> JSONResponse:
        fault = await _apply_fault("contacts.search_duplicate")
        if fault is not None:
            return fault
        params = request.query_params
        email = (params.get("email") or "").lower()
        number = params.get("number") or ""
        for contact in state.contacts.values():
            if contact.get("locationId") != params.get("locationId"):
                continue
            if (email and contact.get("email", "").lower() == email) or (
                number and contact.get("phone") == number
            ):
                return JSONResponse(status_code=200, content={"contact": contact})
        return JSONResponse(status_code=200, content={"contact": None})

    @app.put("/contacts/{contact_id}")
    async def update_contact(contact_id: str, request: Request) -> JSONResponse:
        fault = await _apply_fault("contacts.update")
        if fault is not None:
            return fault
        contact = state.contacts.get(contact_id)
        if contact is None:
            return JSONResponse(status_code=404, content={"message": "Contact not found"})
        contact.update(await request.json())
        return JSONResponse(status_code=200, content={"succeded": True, "contact": contact})

    @app.post("/contacts/{contact_id}/tags")
    async def add_tags(contact_id: str, request: Request) -> JSONResponse:
        fault = await _apply_fault("contacts.tags")
        if fault is not None:
            return fault
        contact = state.contacts.get(contact_id)
        if contact is None:
            return JSONResponse(status_code=404, content={"message": "Contact not found"})
        tags = (await request.json()).get("tags") or []
        merged = sorted(set(contact.get("tags") or []) | set(tags))
        contact["tags"] = merged
        state.tags_added.append({"contactId": contact_id, "tags": tags})
        return JSONResponse(status_code=200, content={"tags": merged})

    @app.post("/contacts/{contact_id}/notes")
    async def add_note(contact_id: str, request: Request) -> JSONResponse:
        fault = await _apply_fault("contacts.notes")
        if fault is not None:
            return fault
        if contact_id not in state.contacts:
            return JSONResponse(status_code=404, content={"message": "Contact not found"})
        note = {
            "id": _new_id("note_"),
            "contactId": contact_id,
            "body": (await request.json()).get("body", ""),
        }
        state.notes.append(note)
        return JSONResponse(status_code=201, content={"note": note})

    # ---- opportunities --------------------------------------------------

    @app.get("/opportunities/search")
    async def search_opportunities(request: Request) -> JSONResponse:
        fault = await _apply_fault("opportunities.search")
        if fault is not None:
            return fault
        params = request.query_params
        found = [
            o
            for o in state.opportunities.values()
            if o.get("contactId") == params.get("contactId")
            and o.get("pipelineId") == params.get("pipelineId")
            and o.get("status") == (params.get("status") or "open")
        ]
        return JSONResponse(
            status_code=200,
            content={"opportunities": found, "meta": {"total": len(found)}},
        )

    @app.post("/opportunities/")
    async def create_opportunity(request: Request) -> JSONResponse:
        fault = await _apply_fault("opportunities.create")
        if fault is not None:
            return fault
        body = await request.json()
        opportunity_id = _new_id("opp_")
        opportunity = {"id": opportunity_id, **body}
        state.opportunities[opportunity_id] = opportunity
        return JSONResponse(status_code=201, content={"opportunity": opportunity})

    @app.put("/opportunities/{opportunity_id}")
    async def update_opportunity(opportunity_id: str, request: Request) -> JSONResponse:
        fault = await _apply_fault("opportunities.update")
        if fault is not None:
            return fault
        opportunity = state.opportunities.get(opportunity_id)
        if opportunity is None:
            return JSONResponse(status_code=404, content={"message": "Opportunity not found"})
        opportunity.update(await request.json())
        return JSONResponse(status_code=200, content={"opportunity": opportunity})

    # ---- conversations & calendar --------------------------------------

    @app.post("/conversations/messages")
    async def send_message(request: Request) -> JSONResponse:
        fault = await _apply_fault("conversations.messages")
        if fault is not None:
            return fault
        body = await request.json()
        message = {"id": _new_id("msg_"), **body}
        state.messages.append(message)
        return JSONResponse(
            status_code=201,
            content={"conversationId": _new_id("conv_"), "messageId": message["id"]},
        )

    @app.post("/calendars/events/appointments")
    async def book_appointment(request: Request) -> JSONResponse:
        fault = await _apply_fault("calendars.appointments")
        if fault is not None:
            return fault
        body = await request.json()
        appointment = {"id": _new_id("appt_"), **body}
        state.appointments.append(appointment)
        return JSONResponse(status_code=201, content={"event": appointment})

    return app


app = create_app()
