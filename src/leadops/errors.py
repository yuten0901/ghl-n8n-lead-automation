"""Error taxonomy.

The distinction that matters operationally is *retryable* vs *permanent*.
A retryable failure goes back on the queue with backoff; a permanent failure goes
straight to the dead-letter store, because retrying it only burns rate limit.
"""

from __future__ import annotations


class LeadOpsError(Exception):
    """Base class for every error this service raises deliberately."""

    retryable: bool = False
    code: str = "leadops_error"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        # `retryable` is on the wire deliberately: the n8n error branch and the
        # webhook caller both decide whether to redeliver from this field, and
        # they should not have to re-derive it from the error code.
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class ValidationFailed(LeadOpsError):
    """Payload could not be normalized into a canonical lead. Never retryable:
    replaying the same malformed body produces the same result."""

    retryable = False
    code = "validation_failed"


class SignatureInvalid(LeadOpsError):
    """Webhook HMAC signature did not verify."""

    retryable = False
    code = "signature_invalid"


class UpstreamTimeout(LeadOpsError):
    """A dependency did not answer within its deadline."""

    retryable = True
    code = "upstream_timeout"


class UpstreamUnavailable(LeadOpsError):
    """Dependency returned 5xx or the connection failed."""

    retryable = True
    code = "upstream_unavailable"


class RateLimited(LeadOpsError):
    """Dependency returned 429. Retryable, but only after `retry_after` seconds."""

    retryable = True
    code = "rate_limited"

    def __init__(
        self, message: str, *, retry_after: float | None = None, detail: dict | None = None
    ) -> None:
        super().__init__(message, detail=detail)
        self.retry_after = retry_after


class UpstreamRejected(LeadOpsError):
    """Dependency returned 4xx that is not 429 — our request is wrong.
    Retrying is pointless and hides the bug."""

    retryable = False
    code = "upstream_rejected"


class AIOutputInvalid(LeadOpsError):
    """The model returned something that is not a valid qualification result,
    and repair did not fix it. Handled by deterministic fallback, not by failing."""

    retryable = False
    code = "ai_output_invalid"
