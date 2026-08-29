"""Deterministic routing: `Qualification` -> `RoutingDecision`.

Two properties this module must have, and is tested for:

* **Total.** Every qualification produces a decision. The `default` rule exists so
  a lead can never fall through into "no stage, no owner, no follow-up" - which
  in a CRM means invisible, which means lost.
* **Pure.** No I/O, no clock, no randomness. The same qualification always routes
  the same way, which is what makes "why did this lead go there?" answerable
  months later from the audit log alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from leadops.models import Qualification, RoutingDecision

DEFAULT_CATEGORIES = (
    "emergency_repair",
    "installation",
    "maintenance",
    "quote_request",
    "warranty_claim",
    "general_enquiry",
    "other",
)


@dataclass(slots=True)
class RoutingTable:
    version: int
    service_categories: tuple[str, ...]
    rules: list[dict[str, Any]]
    default: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> RoutingTable:
        # encoding is pinned: the default differs by OS locale (cp932 on a
        # Japanese Windows box) and a config file is exactly where that bites.
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RoutingTable:
        categories = tuple(raw.get("service_categories") or DEFAULT_CATEGORIES)
        if "other" not in categories:
            # A closed vocabulary with no exit forces unknown values into a
            # wrong-but-valid category, which is worse than an honest "other".
            categories = (*categories, "other")
        default = raw.get("default") or {
            "id": "default_nurture",
            "then": {"pipeline_stage": "nurture", "reason": "No rule matched."},
        }
        return cls(
            version=int(raw.get("version", 1)),
            service_categories=categories,
            rules=list(raw.get("rules") or []),
            default=default,
        )

    def is_known_category(self, category: str) -> bool:
        return category in self.service_categories


def _matches(condition: dict[str, Any], qualification: Qualification) -> bool:
    """All predicates in a `when:` block must hold (AND). An unrecognised
    predicate makes the rule fail closed rather than match by accident - a typo
    in a config file should not silently route every lead to the owner's phone."""
    known = {
        "qualification_score_gte",
        "qualification_score_lt",
        "priority_in",
        "service_category_in",
        "intent_contains",
        "has_missing_information",
    }
    for key in condition:
        if key not in known:
            return False

    floor = condition.get("qualification_score_gte")
    if floor is not None and qualification.qualification_score < int(floor):
        return False

    ceiling = condition.get("qualification_score_lt")
    if ceiling is not None and qualification.qualification_score >= int(ceiling):
        return False

    if "priority_in" in condition and qualification.priority.value not in set(
        condition["priority_in"]
    ):
        return False

    if "service_category_in" in condition and qualification.service_category not in set(
        condition["service_category_in"]
    ):
        return False
    if "intent_contains" in condition:
        needles = condition["intent_contains"]
        needles = [needles] if isinstance(needles, str) else list(needles)
        haystack = qualification.intent.lower()
        if not any(str(n).lower() in haystack for n in needles):
            return False
    if "has_missing_information" in condition:
        expected = bool(condition["has_missing_information"])
        if bool(qualification.missing_information) is not expected:
            return False
    return True


def _decision_from(rule_id: str, then: dict[str, Any]) -> RoutingDecision:
    return RoutingDecision(
        rule_id=rule_id,
        pipeline_stage=str(then.get("pipeline_stage", "new_lead")),
        opportunity_status=str(then.get("opportunity_status", "open")),
        monetary_value=float(then.get("monetary_value", 0) or 0),
        tags=[str(t) for t in (then.get("tags") or [])],
        follow_up_sequence=str(then.get("follow_up_sequence", "") or ""),
        notify_sales=bool(then.get("notify_sales", False)),
        notify_channel=str(then.get("notify_channel", "") or ""),
        sla_minutes=then.get("sla_minutes"),
        reason=str(then.get("reason", "") or ""),
    )


def route(qualification: Qualification, table: RoutingTable) -> RoutingDecision:
    """First match wins; the default always matches."""
    for rule in table.rules:
        rule_id = str(rule.get("id") or "unnamed")
        if _matches(rule.get("when") or {}, qualification):
            return _decision_from(rule_id, rule.get("then") or {})

    default_id = str(table.default.get("id") or "default")
    return _decision_from(default_id, table.default.get("then") or {})


def coerce_category(category: str, table: RoutingTable) -> tuple[str, bool]:
    """Return (category, was_coerced). Anything outside the configured vocabulary
    becomes `other`, so a hallucinated category cannot reach a routing rule."""
    cleaned = str(category or "").strip().lower().replace(" ", "_").replace("-", "_")
    if table.is_known_category(cleaned):
        return cleaned, False
    return "other", True
