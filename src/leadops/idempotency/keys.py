"""Two different keys, for two different duplicate problems.

Conflating them is the usual bug. They are not the same question:

1. `event_idempotency_key` — "have I already processed *this delivery*?"
   Meta retries a webhook, n8n replays an execution, a proxy times out and the
   caller resends. Same event, delivered twice. The correct response is to return
   the *stored* result of the first processing and touch nothing.

2. `lead_identity_key` — "is this the *same person* as a lead I already have?"
   A homeowner fills the website form on Monday and the Google Ads form on Friday.
   Two genuinely different events, one CRM contact. The correct response is to
   update the existing contact and *not* open a second opportunity.

Getting (2) wrong produces the duplicate-contact mess that agencies get hired to
clean up. Getting (1) wrong produces duplicate opportunities and double-texted
leads. This module is small on purpose: it is the part everything else trusts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from leadops.models import CanonicalLead

_HEADER_CANDIDATES = (
    "x-idempotency-key",
    "idempotency-key",
    "x-request-id",
    "x-webhook-id",
    "x-ghl-webhook-id",  # GHL sends a delivery id on its outbound webhooks
    "x-hook-id",
)


# NUL separator: it cannot occur inside any of the inputs, so ("ab", "c") and
# ("a", "bc") cannot be made to hash to the same value.
_SEPARATOR = chr(0)


def _sha(*parts: str) -> str:
    # NUL separator: it cannot appear in any of the inputs, so ("ab", "c")
    # and ("a", "bc") cannot hash to the same value.
    digest = hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()
    return digest[:40]


def event_idempotency_key(
    *,
    source: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    explicit_key: str | None = None,
) -> tuple[str, str]:
    """Return (key, derivation) for a single webhook delivery.

    Preference order, strongest first:
      1. A key the caller supplied explicitly (query param or body field).
      2. A delivery/request id header from the sender.
      3. The source's own event id from the body (`leadgen_id`, `lead_id`, ...).
      4. A hash of the whole body.

    (4) is the fallback and it has a known limitation, stated here rather than
    hidden: two *genuinely distinct* submissions with byte-identical bodies and no
    id and no timestamp collapse into one. In practice payloads carry a timestamp,
    which separates them. Sources without one should send an idempotency header —
    `docs/architecture.md` says so, and `/admin/stats` counts how many events
    relied on the weak derivation.
    """
    normalized_headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    if explicit_key:
        return f"{source}:{str(explicit_key)[:120]}", "explicit"

    for name in _HEADER_CANDIDATES:
        value = normalized_headers.get(name)
        if value:
            return f"{source}:{value[:120]}", f"header:{name}"

    for field in ("event_id", "leadgen_id", "lead_id", "submission_id", "id"):
        value = payload.get(field)
        if value not in (None, ""):
            return f"{source}:{str(value)[:120]}", f"body:{field}"

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{source}:sha256:{_sha(body)}", "body_hash"


def lead_identity_key(lead: CanonicalLead, *, location_id: str) -> str:
    """Stable identity for a *person* within one GHL location.

    Email is preferred over phone because it is the stronger identifier and the
    one GHL's own duplicate search treats as primary. Scoped by location so a
    multi-tenant deployment cannot merge two agencies' contacts.
    """
    if lead.email:
        return f"{location_id}:email:{lead.email}"
    if lead.phone:
        return f"{location_id}:phone:{lead.phone}"
    # normalize_payload() rejects leads with neither, so this is defence in depth.
    return f"{location_id}:external:{lead.source.value}:{lead.external_id}"


def step_key(idempotency_key: str, step_name: str) -> str:
    """Key for one memoized pipeline step within one event."""
    return f"{idempotency_key}#{step_name}"


WEAK_DERIVATIONS = frozenset({"body_hash"})


def is_weak(derivation: str) -> bool:
    """True when the key came from hashing the body rather than from a real id.
    Surfaced in logs and counted by /admin/stats, so the weakness is visible
    rather than assumed away."""
    return derivation in WEAK_DERIVATIONS
