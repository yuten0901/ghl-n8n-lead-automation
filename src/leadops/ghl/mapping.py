"""Translate canonical vocabulary into one GHL sub-account's ids.

Every GHL location has its own custom-field ids, pipeline id, and stage ids. They
are not stable across accounts, which is why a workflow built in a demo account
breaks on the client's account. Keeping the mapping in one JSON file means
onboarding a second location is a config file, not a code change.

`scripts/discover_ghl_ids.py` reads them from a live location and prints a filled
mapping file, so this is a five-minute step rather than a manual hunt through the
GHL UI.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class GHLMapping:
    location_id: str
    pipeline_id: str
    pipeline_stages: dict[str, str] = field(default_factory=dict)
    custom_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    tags: dict[str, Any] = field(default_factory=dict)
    calendar_id: str = ""
    assigned_user_ids: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> GHLMapping:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> GHLMapping:
        return cls(
            location_id=str(raw.get("location_id", "")),
            pipeline_id=str(raw.get("pipeline_id", "")),
            pipeline_stages=dict(raw.get("pipeline_stages") or {}),
            custom_fields=dict(raw.get("custom_fields") or {}),
            tags=dict(raw.get("tags") or {}),
            calendar_id=str(raw.get("calendar_id", "")),
            assigned_user_ids=dict(raw.get("assigned_user_ids") or {}),
        )

    def stage_id(self, stage_name: str) -> str:
        """Unknown stage names fall back to `new_lead` rather than raising.

        The judgement here: a routing rule naming a stage that was renamed in GHL
        should land the lead somewhere a human will see it, not reject the lead.
        The fallback is recorded by the caller so the misconfiguration is visible.
        """
        return self.pipeline_stages.get(stage_name) or self.pipeline_stages.get("new_lead", "")

    def has_stage(self, stage_name: str) -> bool:
        return stage_name in self.pipeline_stages

    def custom_field_payload(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        """Build the `customFields` array the current GHL contract expects.

        Shape: [{"id": "<fieldId>", "fieldValue": "<value>"}]. Fields not present
        in the mapping are skipped silently *by design*: pushing an unknown field
        id is a 422 that fails the whole contact write, and losing one analytics
        field is not worth losing the lead.
        """
        payload: list[dict[str, Any]] = []
        for name, value in values.items():
            field_def = self.custom_fields.get(name)
            if not field_def or value in (None, ""):
                continue
            entry: dict[str, Any] = {"fieldValue": _stringify(value)}
            if field_def.get("id"):
                entry["id"] = field_def["id"]
            elif field_def.get("key"):
                entry["key"] = field_def["key"]
            else:
                continue
            payload.append(entry)
        return payload

    def unmapped_fields(self, values: dict[str, Any]) -> list[str]:
        """Which requested fields had no mapping. Logged, not swallowed."""
        return [name for name in values if name not in self.custom_fields]

    def tags_for(
        self, *, source: str, priority: str, extra: list[str], degraded: bool
    ) -> list[str]:
        """Assemble the tag set. De-duplicated and order-stable so the same lead
        produces the same tags, which keeps GHL workflow triggers predictable."""
        collected: list[str] = list(self.tags.get("always") or [])
        by_source = (self.tags.get("by_source") or {}).get(source)
        if by_source:
            collected.append(by_source)
        by_priority = (self.tags.get("by_priority") or {}).get(priority)
        if by_priority:
            collected.append(by_priority)
        collected.extend(extra)
        if degraded and self.tags.get("degraded"):
            collected.append(self.tags["degraded"])

        seen: set[str] = set()
        ordered: list[str] = []
        for tag in collected:
            cleaned = str(tag).strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                ordered.append(cleaned)
        return ordered

    def user_id(self, channel: str) -> str:
        return self.assigned_user_ids.get(channel, "")


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)
