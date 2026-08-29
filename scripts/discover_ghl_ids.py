"""Read the ids out of a real GoHighLevel sub-account and print a filled mapping.

Every GHL location has its own custom-field ids, pipeline id and stage ids. They
are not portable between accounts, which is the single most common reason a
workflow that "worked in the demo" fails on the client's account. This script
turns the manual hunt through the GHL UI into one command.

    GHL_ACCESS_TOKEN=... GHL_LOCATION_ID=... python scripts/discover_ghl_ids.py \\
        > config/ghl-mapping.json

Requires a real, paid GoHighLevel location. It was written against the documented
v2 endpoints and has NOT been run against a live account in building this
repository - see docs/ghl-integration.md, which lists exactly what to check on
first contact. It is read-only: it issues GET requests and writes nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

BASE = os.environ.get("GHL_BASE_URL", "https://services.leadconnectorhq.com")
VERSION = os.environ.get("GHL_API_VERSION", "2021-07-28")

# Custom fields this system writes. Matched case-insensitively against the
# location's field names, so a client who called it "Lead Score" still matches.
WANTED_FIELDS = {
    "lead_source": ("lead source", "leadsource", "source"),
    "lead_score": ("lead score", "leadscore", "score"),
    "lead_priority": ("lead priority", "priority"),
    "service_category": ("service category", "service type", "category"),
    "ai_summary": ("ai summary", "lead summary", "summary"),
    "missing_information": ("missing information", "missing info"),
    "correlation_id": ("correlation id", "correlationid", "trace id"),
    "qualification_mode": ("qualification mode", "qualification source"),
}

# Stage names this system routes to, and the GHL stage names they usually map to.
WANTED_STAGES = {
    "new_lead": ("new lead", "new leads", "new", "inbound"),
    "needs_info": ("needs info", "needs information", "awaiting info", "qualifying"),
    "hot_lead": ("hot lead", "hot", "priority", "book appointment", "quote"),
    "nurture": ("nurture", "nurturing", "long term", "follow up"),
    "unqualified": ("unqualified", "disqualified", "junk", "lost"),
}


def _match(name: str, candidates: tuple[str, ...]) -> bool:
    lowered = name.strip().lower()
    return any(candidate in lowered for candidate in candidates)


async def fetch(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    response = await client.get(path, params=params)
    if response.status_code >= 400:
        print(
            f"  ! GET {path} returned {response.status_code}: {response.text[:200]}",
            file=sys.stderr,
        )
        return {}
    return response.json()


async def main() -> int:
    token = os.environ.get("GHL_ACCESS_TOKEN", "")
    location_id = os.environ.get("GHL_LOCATION_ID", "")
    if not token or not location_id:
        print(
            "Set GHL_ACCESS_TOKEN and GHL_LOCATION_ID.\n"
            "Both come from a Private Integration token on the sub-account\n"
            "(Settings -> Private Integrations), or from an OAuth install.",
            file=sys.stderr,
        )
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
        "Version": VERSION,
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(base_url=BASE, headers=headers, timeout=20.0) as client:
        print(f"Reading location {location_id} from {BASE} ...", file=sys.stderr)

        fields_body = await fetch(client, f"/locations/{location_id}/customFields", {})
        pipelines_body = await fetch(
            client, "/opportunities/pipelines", {"locationId": location_id}
        )
        calendars_body = await fetch(client, "/calendars/", {"locationId": location_id})
        users_body = await fetch(client, "/users/", {"locationId": location_id})

    # --- custom fields ----------------------------------------------------
    custom_fields: dict[str, dict[str, str]] = {}
    unmatched: list[str] = []
    for field in fields_body.get("customFields") or []:
        name = str(field.get("name", ""))
        for key, candidates in WANTED_FIELDS.items():
            if key not in custom_fields and _match(name, candidates):
                custom_fields[key] = {
                    "id": str(field.get("id", "")),
                    "key": str(field.get("fieldKey", "")),
                }
                break
        else:
            unmatched.append(name)

    missing_fields = sorted(set(WANTED_FIELDS) - set(custom_fields))

    # --- pipeline + stages ------------------------------------------------
    pipelines = pipelines_body.get("pipelines") or []
    pipeline = pipelines[0] if pipelines else {}
    stages_by_name = {
        str(stage.get("name", "")): str(stage.get("id", ""))
        for stage in (pipeline.get("stages") or [])
    }
    pipeline_stages: dict[str, str] = {}
    for key, candidates in WANTED_STAGES.items():
        for stage_name, stage_id in stages_by_name.items():
            if _match(stage_name, candidates):
                pipeline_stages[key] = stage_id
                break

    missing_stages = sorted(set(WANTED_STAGES) - set(pipeline_stages))

    calendars = calendars_body.get("calendars") or []
    users = users_body.get("users") or []

    mapping = {
        "location_id": location_id,
        "pipeline_id": str(pipeline.get("id", "")),
        "pipeline_stages": pipeline_stages,
        "custom_fields": custom_fields,
        "tags": {
            "always": ["leadops"],
            "by_source": {
                "website": "source-website",
                "meta": "source-meta",
                "google": "source-google",
                "partner": "source-partner",
            },
            "by_priority": {
                "high": "priority-high",
                "medium": "priority-medium",
                "low": "priority-low",
            },
            "degraded": "ai-fallback-used",
        },
        "calendar_id": str(calendars[0].get("id", "")) if calendars else "",
        "assigned_user_ids": {
            "sales_primary": str(users[0].get("id", "")) if users else "",
            "sales_overflow": str(users[1].get("id", "")) if len(users) > 1 else "",
        },
    }

    # Diagnostics go to stderr so stdout stays a clean, redirectable JSON file.
    print(f"\nPipelines found: {len(pipelines)}", file=sys.stderr)
    if len(pipelines) > 1:
        print(
            "  ! More than one pipeline; the first was used. Pick deliberately:\n    "
            + "\n    ".join(f"{p.get('name')} = {p.get('id')}" for p in pipelines),
            file=sys.stderr,
        )
    if missing_stages:
        print(
            f"  ! No stage matched: {', '.join(missing_stages)}\n"
            f"    Stages in this pipeline: {', '.join(stages_by_name) or '(none)'}\n"
            "    Create them, or edit config/routing.yml to use the names that exist.",
            file=sys.stderr,
        )
    if missing_fields:
        print(
            f"  ! No custom field matched: {', '.join(missing_fields)}\n"
            "    Create them on the location as Text fields. Unmapped fields are\n"
            "    skipped at write time rather than failing the contact, so the\n"
            "    integration still works - it just records less.",
            file=sys.stderr,
        )
    if unmatched:
        print(f"  (ignored {len(unmatched)} other custom fields)", file=sys.stderr)

    print(json.dumps(mapping, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
