"""Qualify a lead: call the model, validate hard, repair once, fall back always.

The contract this module offers the pipeline is worth stating precisely, because
it is the reason an LLM can sit in the middle of a revenue path at all:

    qualify() always returns a valid Qualification. It never raises.

Everything that can go wrong with a model - timeout, 500, refusal, prose instead
of JSON, markdown fences, a score of 250, an invented service category - is
handled here and ends in a deterministic result marked `degraded=True`. The
degradation is recorded in the audit log and written to the CRM as
`qualification_mode`, so "the AI was down for an hour" is a question the data can
answer rather than a mystery about why Tuesday's leads look odd.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from pydantic import ValidationError

from leadops.ai import rules
from leadops.ai.providers import Provider
from leadops.ai.schema import REPAIR_PROMPT, SYSTEM_PROMPT, build_user_prompt
from leadops.errors import LeadOpsError
from leadops.models import CanonicalLead, Qualification
from leadops.routing.rules import RoutingTable, coerce_category

# Models wrap JSON in ```json fences often enough that stripping them is standard
# hygiene, not a workaround. Anything beyond this is a real validation failure.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(slots=True)
class QualifyOutcome:
    qualification: Qualification
    attempts: int
    duration_ms: float
    provider_used: str
    input_tokens: int = 0
    output_tokens: int = 0
    notes: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = []


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Tolerates code fences and leading/trailing prose, because both happen. Does
    not tolerate anything else: if there is no parseable object, that is a
    validation failure and the caller repairs or falls back.
    """
    if not text or not text.strip():
        raise ValueError("empty model response")

    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in model response") from None
        parsed = json.loads(candidate[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError(f"model returned {type(parsed).__name__}, expected object")
    return parsed


def _lead_for_prompt(lead: CanonicalLead) -> dict:
    """What the model is allowed to see.

    Deliberately narrow. The model does not need the email address or the phone
    number to classify intent, so it does not get them - that is one fewer place
    customer PII travels, and it removes a class of "the model echoed the
    customer's phone number into the summary" incident. Presence is passed as a
    boolean because completeness genuinely affects the score.
    """
    return {
        "source": lead.source.value,
        "service_field": lead.service,
        "message": lead.message,
        "service_area": lead.location,
        "has_phone": bool(lead.phone),
        "has_email": bool(lead.email),
        "has_name": bool(lead.first_name or lead.last_name),
        "submitted_at": lead.submitted_at.isoformat(),
    }


def _finalise(
    qualification: Qualification,
    *,
    table: RoutingTable,
    provider: str,
    model: str,
    degraded: bool,
    reason: str,
    notes: list[str],
) -> Qualification:
    """Clamp a validated result into something safe to route on."""
    category, coerced = coerce_category(qualification.service_category, table)
    if coerced:
        notes.append(
            f"service_category '{qualification.service_category}' not in vocabulary -> other"
        )
    return qualification.model_copy(
        update={
            "service_category": category,
            "qualification_score": max(0, min(100, qualification.qualification_score)),
            "degraded": degraded,
            "degraded_reason": reason,
            "provider": provider,
            "model": model,
        }
    )


async def qualify(
    lead: CanonicalLead,
    *,
    provider: Provider,
    table: RoutingTable,
    max_attempts: int = 2,
) -> QualifyOutcome:
    """Never raises. See the module docstring for why that is the contract."""
    started = time.perf_counter()
    notes: list[str] = []

    # Input-side check, before any provider is asked and regardless of which one
    # is configured. A lead whose body is shaped like instructions to a model is
    # not scored by a model at all - there is nothing to gain from asking, and a
    # compliant model is exactly the case this has to survive.
    #
    # This used to sit inside rules.classify(), which meant it only ever ran on
    # the offline path. On the live-model path an injected lead could come back
    # as emergency_repair / high / 100 and route straight to sales with a
    # five-minute SLA. See docs/ai-qualification.md#what-is-actually-guaranteed.
    if rules.looks_like_injection(lead):
        return QualifyOutcome(
            qualification=_finalise(
                rules.injection_verdict(),
                table=table,
                provider="deterministic",
                model="injection-guard",
                degraded=True,
                reason="lead body contains instruction-like text; model not consulted",
                notes=notes,
            ),
            attempts=0,
            duration_ms=(time.perf_counter() - started) * 1000,
            provider_used="injection-guard",
            notes=notes,
        )

    if provider.name == "deterministic":
        result = rules.classify(lead)
        return QualifyOutcome(
            qualification=_finalise(
                result,
                table=table,
                provider="deterministic",
                model="rules-v1",
                degraded=False,
                reason="",
                notes=notes,
            ),
            attempts=0,
            duration_ms=(time.perf_counter() - started) * 1000,
            provider_used="deterministic",
            notes=notes,
        )

    user_prompt = build_user_prompt(_lead_for_prompt(lead))
    repair: str | None = None
    last_error = ""
    input_tokens = output_tokens = 0
    attempts = 0
    last_raw_text = ""

    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        try:
            reply = await provider.complete(system=SYSTEM_PROMPT, user=user_prompt, repair=repair)
            input_tokens += reply.input_tokens
            output_tokens += reply.output_tokens
            last_raw_text = reply.text

            parsed = extract_json(reply.text)
            validated = Qualification.model_validate(
                {k: v for k, v in parsed.items() if k in Qualification.model_fields}
            )
            return QualifyOutcome(
                qualification=_finalise(
                    validated,
                    table=table,
                    provider=reply.provider,
                    model=reply.model,
                    degraded=False,
                    reason="",
                    notes=notes,
                ),
                attempts=attempt,
                duration_ms=(time.perf_counter() - started) * 1000,
                provider_used=reply.provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                notes=notes,
            )

        except (ValueError, ValidationError) as exc:
            # Malformed output. One repair turn, then the rules engine.
            last_error = f"{type(exc).__name__}: {exc}"[:400]
            notes.append(f"attempt {attempt}: invalid structured output")
            repair = REPAIR_PROMPT.format(error=last_error, previous=last_raw_text[:1500])

        except LeadOpsError as exc:
            # Transport failure. Retrying inside the model call is the provider's
            # job; here a failure means we stop asking and score it ourselves,
            # because a lead waiting on a retry loop is a lead going cold.
            last_error = f"{exc.code}: {exc.message}"[:400]
            notes.append(f"attempt {attempt}: {exc.code}")
            break

        except Exception as exc:  # noqa: BLE001 - the whole point is to not propagate
            last_error = f"unexpected {type(exc).__name__}: {exc}"[:400]
            notes.append(f"attempt {attempt}: unexpected provider error")
            break

    fallback = rules.classify(lead)
    return QualifyOutcome(
        qualification=_finalise(
            fallback,
            table=table,
            provider="deterministic",
            model="rules-v1",
            degraded=True,
            reason=last_error or "model produced no usable result",
            notes=notes,
        ),
        attempts=attempts,
        duration_ms=(time.perf_counter() - started) * 1000,
        provider_used="deterministic-fallback",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        notes=notes,
    )
