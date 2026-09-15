"""GoHighLevel client for the current documented API contract.

Scope of the claim, stated plainly: this is an **implemented integration
interface** refreshed against the documented `v3` API, exercised end-to-end against
the bundled mock in `mock/`, and verified for contact/opportunity behavior in an
official HighLevel Sandbox. It has not been run against a paid GHL location. See
`docs/ghl-integration.md` for the exact request/response shapes and for the short
list of things that must be re-verified on first contact with a real sub-account.

API surface used (base `https://services.leadconnectorhq.com`):

    POST /contacts/upsert                  create-or-update by email/phone
    GET  /contacts/search/duplicate        duplicate lookup within a location
    PUT  /contacts/{contactId}             field/tag updates
    POST /contacts/{contactId}/tags        add tags
    POST /contacts/{contactId}/notes       write the AI summary onto the contact
    GET  /opportunities/search             find an existing open opportunity
    POST /opportunities/                   create in a pipeline stage
    PUT  /opportunities/{id}               move stage / update value
    POST /conversations/messages           SMS / Email follow-up
    POST /calendars/events/appointments    booking

Every request carries `Authorization: Bearer <token>` and `Version: v3`.
The version header is not optional and pinning it is deliberate: GHL ships
breaking changes behind new version dates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from leadops.errors import (
    RateLimited,
    UpstreamRejected,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from leadops.reliability.retry import Attempted, RetryPolicy, call_with_retry

# GHL rate limits are per location: a 100-request burst per 10 seconds and a
# daily ceiling. 429 therefore has to be a first-class outcome, not an exception
# case, for any agency running more than a trickle of leads.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class GHLResponse:
    status_code: int
    body: dict[str, Any]
    attempts: int = 1
    duration_ms: float = 0.0


@dataclass
class GHLClient:
    """Thin, retrying, fully typed wrapper. Holds no business logic on purpose —
    the decisions about *what* to write live in `leadops.ghl.operations`."""

    base_url: str
    access_token: str
    location_id: str
    api_version: str = "v3"
    timeout_seconds: float = 10.0
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    client: httpx.AsyncClient | None = None
    # Set by the pipeline so every outbound call can be traced back to a lead.
    correlation_id: str = ""

    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Version": self.api_version,
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.correlation_id:
            # Not a GHL feature; it makes our own request/response logs joinable
            # and any reverse proxy in between will pass it through.
            headers["X-Correlation-Id"] = self.correlation_id
        return headers

    async def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_seconds)
        return self.client

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    # -- transport ---------------------------------------------------------

    def _classify(self, response: httpx.Response) -> None:
        """Map an HTTP status onto our retryable/permanent taxonomy."""
        if response.status_code < 400:
            return

        body: dict[str, Any]
        try:
            parsed = response.json()
            body = parsed if isinstance(parsed, dict) else {"raw": parsed}
        except ValueError:
            body = {"raw": response.text[:500]}

        detail = {"status_code": response.status_code, "body": body}

        if response.status_code == 429:
            header = response.headers.get("Retry-After")
            retry_after: float | None = None
            if header:
                try:
                    retry_after = float(header)
                except ValueError:
                    retry_after = None
            raise RateLimited(
                "GoHighLevel rate limit reached", retry_after=retry_after, detail=detail
            )
        if response.status_code in RETRYABLE_STATUS:
            raise UpstreamUnavailable(f"GoHighLevel returned {response.status_code}", detail=detail)
        if response.status_code == 401:
            # Retrying an expired token just fails four times. In a real
            # deployment this is the signal to run the OAuth refresh flow
            # (docs/ghl-integration.md#oauth); here it is a loud permanent error.
            raise UpstreamRejected("GoHighLevel rejected the credentials (401)", detail=detail)
        raise UpstreamRejected(
            f"GoHighLevel rejected the request ({response.status_code})", detail=detail
        )

    async def request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> GHLResponse:
        async def once() -> GHLResponse:
            http = await self._http()
            started = time.perf_counter()
            try:
                response = await http.request(
                    method, path, json=json, params=params, headers=self.headers()
                )
            except httpx.TimeoutException as exc:
                raise UpstreamTimeout(
                    f"GoHighLevel timed out after {self.timeout_seconds}s",
                    detail={"path": path, "method": method},
                ) from exc
            except httpx.HTTPError as exc:
                raise UpstreamUnavailable(
                    f"Could not reach GoHighLevel: {type(exc).__name__}",
                    detail={"path": path, "method": method},
                ) from exc

            self._classify(response)

            try:
                parsed = response.json()
            except ValueError:
                parsed = {}
            return GHLResponse(
                status_code=response.status_code,
                body=parsed if isinstance(parsed, dict) else {"data": parsed},
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        attempted: Attempted = await call_with_retry(once, self.policy)
        result: GHLResponse = attempted.value  # type: ignore[assignment]
        result.attempts = attempted.attempts
        result.duration_ms = attempted.duration_ms
        return result

    # -- contacts ----------------------------------------------------------

    async def upsert_contact(self, payload: dict) -> GHLResponse:
        """`POST /contacts/upsert` — GHL matches on email, then phone, within the
        location, and creates only if neither matches. Using upsert instead of
        create-then-catch-409 is what keeps a returning lead from becoming a
        second contact."""
        body = {"locationId": self.location_id, **payload}
        return await self.request("POST", "/contacts/upsert", json=body)

    async def find_duplicate_contact(self, *, email: str = "", phone: str = "") -> GHLResponse:
        params: dict[str, str] = {"locationId": self.location_id}
        if email:
            params["email"] = email
        if phone:
            params["number"] = phone
        return await self.request("GET", "/contacts/search/duplicate", params=params)

    async def update_contact(self, contact_id: str, payload: dict) -> GHLResponse:
        return await self.request("PUT", f"/contacts/{contact_id}", json=payload)

    async def add_tags(self, contact_id: str, tags: list[str]) -> GHLResponse:
        return await self.request("POST", f"/contacts/{contact_id}/tags", json={"tags": tags})

    async def add_note(self, contact_id: str, body: str) -> GHLResponse:
        return await self.request("POST", f"/contacts/{contact_id}/notes", json={"body": body})

    # -- opportunities -----------------------------------------------------

    async def search_opportunities(
        self, *, contact_id: str, pipeline_id: str, status: str = "open"
    ) -> GHLResponse:
        return await self.request(
            "GET",
            "/opportunities/search",
            params={
                "locationId": self.location_id,
                "contactId": contact_id,
                "pipelineId": pipeline_id,
                "status": status,
            },
        )

    async def create_opportunity(self, payload: dict) -> GHLResponse:
        body = {"locationId": self.location_id, **payload}
        return await self.request("POST", "/opportunities/", json=body)

    async def update_opportunity(self, opportunity_id: str, payload: dict) -> GHLResponse:
        return await self.request("PUT", f"/opportunities/{opportunity_id}", json=payload)

    # -- conversations & calendar -----------------------------------------

    async def send_message(self, payload: dict) -> GHLResponse:
        """`POST /conversations/messages` — type is SMS or Email."""
        return await self.request("POST", "/conversations/messages", json=payload)

    async def book_appointment(self, payload: dict) -> GHLResponse:
        body = {"locationId": self.location_id, **payload}
        return await self.request("POST", "/calendars/events/appointments", json=body)
