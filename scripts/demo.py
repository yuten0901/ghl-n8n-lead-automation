"""One-command demo: eight scenarios, no credentials, no services.

Runs the real pipeline against the real mock GoHighLevel server, in process, and
prints what ended up in the CRM after each scenario. This is the fastest way for
a reviewer to see that the reliability claims in the README are implemented
rather than described.

    python scripts/demo.py

Every scenario below is also an automated test; this script exists so the
behaviour is visible without reading the test suite.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from mock.ghl_mock import server as mock  # noqa: E402

from leadops.config import Settings  # noqa: E402
from leadops.ghl.client import GHLClient  # noqa: E402
from leadops.ghl.mapping import GHLMapping  # noqa: E402
from leadops.logging_setup import configure_logging  # noqa: E402
from leadops.pipeline import process_lead  # noqa: E402
from leadops.reliability.retry import RetryPolicy  # noqa: E402
from leadops.routing.rules import RoutingTable  # noqa: E402
from leadops.storage import create_all, init_engine, session_scope  # noqa: E402
from leadops.storage.db import reset_for_tests  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = REPO / "n8n" / "fixtures"

GREEN, YELLOW, RED, DIM, BOLD, RESET = (
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def heading(number: int, title: str, why: str) -> None:
    print(f"\n{BOLD}[{number}] {title}{RESET}")
    print(f"{DIM}    {why}{RESET}")


def show(result) -> None:
    colour = {"succeeded": GREEN, "duplicate": YELLOW}.get(result.status.value, RED)
    print(f"    status          {colour}{result.status.value}{RESET}")
    if result.qualification:
        q = result.qualification
        flag = f" {YELLOW}(fallback){RESET}" if q.degraded else ""
        print(
            f"    qualification   {q.service_category} / {q.priority.value} / "
            f"score {q.qualification_score}{flag}"
        )
    if result.routing:
        r = result.routing
        print(
            f"    routed by       {r.rule_id} -> stage '{r.pipeline_stage}'"
            f"{', notify sales' if r.notify_sales else ''}"
        )
        print(f"    because         {DIM}{r.reason}{RESET}")
    if result.error:
        print(
            f"    error           {RED}{result.error['code']}{RESET} "
            f"(retryable={result.error.get('retryable')}, "
            f"terminal={result.error.get('terminal')})"
        )


def crm_state() -> str:
    return (
        f"    CRM now         {len(mock.state.contacts)} contact(s), "
        f"{len(mock.state.opportunities)} opportunity(ies), "
        f"{len(mock.state.messages)} message(s) sent"
    )


async def main() -> int:
    configure_logging("WARNING", "text")  # keep the demo output readable

    workdir = tempfile.mkdtemp(prefix="leadops-demo-")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{workdir}/demo.sqlite3",
        ghl_base_url="http://ghl.mock",
        ghl_access_token="demo_token_not_a_real_secret",
        ghl_location_id="loc_DEMO0000000000000000",
        ghl_pipeline_id="pipe_DEMO000000000000000",
        ai_provider="deterministic",
        ghl_max_attempts=4,
        ghl_backoff_base_seconds=0.05,
        ghl_backoff_max_seconds=0.2,
        max_delivery_attempts=3,
        routing_config_path=str(REPO / "config" / "routing.yml"),
        ghl_mapping_path=str(REPO / "config" / "ghl-mapping.json"),
    )

    await reset_for_tests()
    init_engine(settings.database_url)
    await create_all()
    mock.state.reset()

    mapping = GHLMapping.load(settings.ghl_mapping_path)
    table = RoutingTable.load(settings.routing_config_path)

    def client() -> GHLClient:
        return GHLClient(
            base_url="http://ghl.mock",
            access_token=settings.ghl_access_token,
            location_id=settings.ghl_location_id,
            client=httpx.AsyncClient(
                transport=httpx.ASGITransport(app=mock.app),
                base_url="http://ghl.mock",
                timeout=10.0,
            ),
            policy=RetryPolicy(max_attempts=4, base_seconds=0.05, max_seconds=0.2),
        )

    async def deliver(source: str, payload: dict, **kwargs):
        async with session_scope() as session:
            return await process_lead(
                source=source,
                payload=payload,
                headers={},
                settings=settings,
                session=session,
                ghl=client(),
                mapping=mapping,
                table=table,
                **kwargs,
            )

    print(f"{BOLD}GoHighLevel + n8n lead automation - local demo{RESET}")
    print(
        f"{DIM}No credentials, no network, no Docker. GHL is the mock in mock/."
        f"\nAI provider: deterministic rules "
        f"(set AI_PROVIDER + AI_API_KEY for a live model).{RESET}"
    )

    # ------------------------------------------------------------------
    heading(
        1,
        "Emergency lead from a website form",
        "High intent, complete details -> hot pipeline stage, SMS, sales alert.",
    )
    result = await deliver("website", fixture("website-lead-emergency.json"))
    show(result)
    print(crm_state())

    # ------------------------------------------------------------------
    heading(
        2,
        "The same webhook delivered four more times",
        "The single most common real-world problem. Nothing must be written twice.",
    )
    for _ in range(4):
        duplicate = await deliver("website", fixture("website-lead-emergency.json"))
    show(duplicate)
    print(crm_state())
    print(
        f"    {GREEN}GHL contact upserts: "
        f"{mock.state.call_counts['contacts.upsert']} (not 5){RESET}"
    )

    # ------------------------------------------------------------------
    heading(
        3,
        "Meta Lead Ads, then Google Ads, for the same person",
        "Two genuinely different events, one human. One contact, one opportunity.",
    )
    meta = fixture("meta-lead-ads.json")
    await deliver("meta", meta)
    google = fixture("google-lead-form.json")
    google["user_column_data"] = [
        {"column_id": "EMAIL", "string_value": "priya.r@example.net"},
        {"column_id": "FULL_NAME", "string_value": "Priya Raghunathan"},
        {"column_id": "SERVICE", "string_value": "New AC installation"},
    ]
    result = await deliver("google", google)
    show(result)
    print(f"    contacts        {len(mock.state.contacts)} (website lead + this person, not four)")

    # ------------------------------------------------------------------
    heading(
        4,
        "A lead with no email and no phone",
        "Dead-lettered, never written to the CRM. Over HTTP this answers 400 "
        "rather than 500, because no redelivery of that body can succeed.",
    )
    result = await deliver("website", fixture("invalid-lead-no-contact.json"))
    show(result)

    # ------------------------------------------------------------------
    heading(
        5,
        "Marketing spam through the contact form",
        "Filed for audit, scored out, and deliberately never messaged.",
    )
    before = len(mock.state.messages)
    result = await deliver("website", fixture("spam-lead.json"))
    show(result)
    print(
        f"    messages sent   {len(mock.state.messages) - before} "
        f"{GREEN}(no customer contact){RESET}"
    )

    # ------------------------------------------------------------------
    heading(
        6,
        "A lead whose message tries to instruct the AI",
        "The model classifies; deterministic rules route. Injection cannot escalate.",
    )
    result = await deliver("website", fixture("prompt-injection-lead.json"))
    show(result)

    # ------------------------------------------------------------------
    heading(
        7,
        "GoHighLevel returns 500 twice, then recovers",
        "Retry with backoff. The lead still lands, and only one contact is created.",
    )
    mock.state.faults["contacts.upsert"] = mock.Fault(mode="500", remaining=2)
    contacts_before = len(mock.state.contacts)
    result = await deliver("partner", fixture("partner-lead.json"))
    show(result)
    print(
        f"    upsert attempts {mock.state.call_counts['contacts.upsert']} total; "
        f"contacts created this scenario: {len(mock.state.contacts) - contacts_before}"
    )

    # ------------------------------------------------------------------
    heading(
        8,
        "Contact succeeds, then the opportunity write fails permanently",
        "Partial failure. The retry must resume, not restart - no duplicate contact.",
    )
    mock.state.faults["opportunities.create"] = mock.Fault(mode="500", remaining=99)
    payload = fixture("website-lead-standard.json")
    failed = await deliver("website", payload)
    show(failed)
    print(
        f"    {DIM}contact created, opportunity missing, customer NOT messaged: "
        f"{len(mock.state.contacts)} contacts / "
        f"{len(mock.state.opportunities)} opportunities{RESET}"
    )

    upserts_before = mock.state.call_counts["contacts.upsert"]
    mock.state.faults.clear()
    print(f"{DIM}    ...the outage ends; the sender redelivers the same webhook...{RESET}")
    recovered = await deliver("website", payload)
    show(recovered)
    print(
        f"    {GREEN}contact re-created? no - upsert calls unchanged "
        f"({mock.state.call_counts['contacts.upsert'] == upserts_before}){RESET}"
    )
    print(crm_state())

    # ------------------------------------------------------------------
    print(f"\n{BOLD}Final CRM state{RESET}")
    for contact in mock.state.contacts.values():
        fields = {
            f.get("id", f.get("key")): f["fieldValue"] for f in contact.get("customFields", [])
        }
        print(
            f"  - {contact.get('name', '?'):<24} "
            f"score {fields.get('cf_DEMO_lead_score_000', '?'):>3}  "
            f"tags: {', '.join(contact.get('tags', []))}"
        )
    print(f"\n{DIM}Everything above is asserted in tests/. Run: pytest -q{RESET}")

    await reset_for_tests()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
