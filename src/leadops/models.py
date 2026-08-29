"""Canonical domain models.

Every lead source is normalized into `CanonicalLead` before anything else runs.
Downstream code (idempotency, AI, GHL, routing) only ever sees this shape, which
is what keeps "add a new lead source" a one-adapter change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LeadSource(StrEnum):
    WEBSITE = "website"
    META = "meta"
    GOOGLE = "google"
    PARTNER = "partner"
    UNKNOWN = "unknown"


class Priority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"
    DUPLICATE = "duplicate"


class CanonicalLead(BaseModel):
    """The one lead shape the rest of the system understands."""

    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(
        description="Source-assigned id; used for event identity when present."
    )
    source: LeadSource
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""  # E.164 when parseable, otherwise the cleaned original
    service: str = ""
    message: str = ""
    location: str = ""
    submitted_at: datetime
    # Anything the adapter could not map. Kept, never silently dropped, because
    # "the field the client cares about" is usually in here on the first call.
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("submitted_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=UTC) if v.tzinfo is None else v.astimezone(UTC)

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or (self.email.split("@")[0] if self.email else "Unknown lead")

    def has_contactable_identity(self) -> bool:
        return bool(self.email or self.phone)


class Qualification(BaseModel):
    """Structured AI output. Enforced by schema, never trusted as free text."""

    model_config = ConfigDict(extra="forbid")

    intent: str
    service_category: str
    priority: Priority
    qualification_score: int = Field(ge=0, le=100)
    missing_information: list[str] = Field(default_factory=list)
    summary: str
    recommended_action: str
    # Operational metadata, set by us and not by the model.
    degraded: bool = False
    degraded_reason: str = ""
    provider: str = "deterministic"
    model: str = ""

    @field_validator("missing_information")
    @classmethod
    def _cap_list(cls, v: list[str]) -> list[str]:
        return [str(x)[:120] for x in v][:10]

    @field_validator("summary", "recommended_action", "intent", "service_category")
    @classmethod
    def _cap_text(cls, v: str) -> str:
        return str(v).strip()[:600]


class RoutingDecision(BaseModel):
    """Where the lead goes. Derived deterministically from `Qualification` — the
    model classifies, the rules route. See docs/architecture.md#prompt-injection."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    pipeline_stage: str
    opportunity_status: str = "open"
    monetary_value: float = 0.0
    tags: list[str] = Field(default_factory=list)
    follow_up_sequence: str = ""
    notify_sales: bool = False
    notify_channel: str = ""
    sla_minutes: int | None = None
    reason: str = ""


class StepResult(BaseModel):
    """One completed pipeline step, persisted so a retry can skip it.

    This is what stops a partial failure (contact created, opportunity failed)
    from creating a duplicate contact on the next attempt.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    attempts: int = 1
    duration_ms: int = 0


class ProcessingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    event_id: str
    idempotency_key: str
    status: EventStatus
    duplicate_of: str | None = None
    lead: CanonicalLead | None = None
    qualification: Qualification | None = None
    routing: RoutingDecision | None = None
    contact_id: str | None = None
    opportunity_id: str | None = None
    steps: list[StepResult] = Field(default_factory=list)
    error: dict[str, Any] | None = None

    def public_response(self) -> dict[str, Any]:
        """What the webhook caller (n8n) gets back. Deliberately small: n8n
        branches on `status` and `routing`, and does not need our internals."""
        return {
            "status": self.status.value,
            "correlation_id": self.correlation_id,
            "event_id": self.event_id,
            "idempotency_key": self.idempotency_key,
            "duplicate_of": self.duplicate_of,
            "contact_id": self.contact_id,
            "opportunity_id": self.opportunity_id,
            "qualification": (
                self.qualification.model_dump(mode="json") if self.qualification else None
            ),
            "routing": self.routing.model_dump(mode="json") if self.routing else None,
            "error": self.error,
        }
