"""Webhook signature verification.

Three properties, each of which is a real attack if missing:

* **Constant-time comparison.** `hmac.compare_digest`, not `==`. String equality
  short-circuits and leaks the signature byte by byte to a patient attacker.
* **The signature covers a timestamp.** Signing only the body means a valid
  request captured once can be replayed forever. The signed string is
  `{timestamp}.{body}` and timestamps outside the skew window are rejected.
* **The raw body is verified, not the parsed one.** `json.dumps(json.loads(x))`
  is not `x`, so re-serialising before verifying breaks on key order and
  whitespace. The route hands us `bytes`.

Scheme: `X-Signature: t=<unix>,v1=<hex hmac-sha256>` - the same shape Stripe and
several other providers use, chosen because it is well understood and easy for a
client's existing sender to produce.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

from leadops.errors import SignatureInvalid


@dataclass(slots=True)
class SignatureCheck:
    verified: bool
    reason: str = ""


def sign(body: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Produce a header value. Used by tests, `scripts/send_lead.py`, and the
    documentation - one implementation, so the docs cannot drift from the check."""
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _parse(header: str) -> tuple[int | None, str]:
    timestamp: int | None = None
    signature = ""
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1":
            signature = value
    return timestamp, signature


def verify(
    body: bytes,
    header: str | None,
    secret: str,
    *,
    max_skew_seconds: int = 300,
    required: bool = True,
    now: float | None = None,
) -> SignatureCheck:
    """Verify a signature header.

    When no secret is configured the request is accepted but reported as
    unverified, and that state is persisted on the event row. The alternative -
    quietly returning `verified=True` when there is nothing to verify against -
    is how a deployment ends up believing it is authenticated when it is not.
    """
    if not secret:
        if required:
            raise SignatureInvalid("Signature required but no signing secret is configured")
        return SignatureCheck(verified=False, reason="no_secret_configured")

    if not header:
        if required:
            raise SignatureInvalid("Missing signature header")
        return SignatureCheck(verified=False, reason="missing_header")

    timestamp, provided = _parse(header)
    if timestamp is None or not provided:
        raise SignatureInvalid("Malformed signature header")

    current = time.time() if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        raise SignatureInvalid(
            "Signature timestamp outside the accepted window",
            detail={"skew_seconds": int(abs(current - timestamp)), "max": max_skew_seconds},
        )

    expected = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise SignatureInvalid("Signature does not match")

    return SignatureCheck(verified=True)
