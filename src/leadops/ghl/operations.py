"""What we actually write into the CRM, and in what order.

The ordering is not arbitrary. It is chosen so that every prefix of it is a sane
state to be interrupted in:

    1. contact upsert      -> the person exists and is contactable
    2. tags                -> GHL workflows can trigger
    3. custom fields       -> score/summary visible on the contact record
    4. note                -> the AI summary is readable by a salesperson
    5. opportunity         -> the deal appears on the pipeline board
    6. follow-up / notify  -> outbound contact happens

If the process dies after step 1, a human sees an untagged contact - untidy but
recoverable. If the order were reversed and it died after sending the follow-up
SMS, the customer has been texted about a job that exists nowhere in the CRM.
Side effects that reach the customer go last, on purpose.

Every step is memoized by the pipeline, so a retry resumes rather than repeats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from leadops.ghl.client import GHLClient, GHLResponse
from leadops.ghl.mapping import GHLMapping
from leadops.models import CanonicalLead, Qualification, RoutingDecision


@dataclass(slots=True)
class ContactResult:
    contact_id: str
    created: bool
    raw: dict[str, Any] = field(default_factory=dict)


def _extract_contact_id(body: dict) -> str:
    """GHL's contact endpoints have not been perfectly consistent about whether
    the object is at the top level or under `contact`. Handling both is cheaper
    than being wrong on the client's account."""
    contact = body.get("contact") if isinstance(body.get("contact"), dict) else body
    return str(contact.get("id") or contact.get("contactId") or "")


def _extract_opportunity_id(body: dict) -> str:
    opp = body.get("opportunity") if isinstance(body.get("opportunity"), dict) else body
    return str(opp.get("id") or opp.get("opportunityId") or "")


def build_contact_payload(
    lead: CanonicalLead,
    qualification: Qualification,
    routing: RoutingDecision,
    mapping: GHLMapping,
    *,
    correlation_id: str,
) -> dict[str, Any]:
    """The `POST /contacts/upsert` body.

    `source` is set from the lead's origin so GHL's own attribution reporting is
    correct - clients ask "where did this lead come from?" inside GHL, not in our
    database, and an attribution field that says "API" for everything is useless.
    """
    payload: dict[str, Any] = {
        "firstName": lead.first_name,
        "lastName": lead.last_name,
        "name": lead.full_name,
        "source": f"leadops:{lead.source.value}",
        "tags": mapping.tags_for(
            source=lead.source.value,
            priority=qualification.priority.value,
            extra=routing.tags,
            degraded=qualification.degraded,
        ),
    }
    if lead.email:
        payload["email"] = lead.email
    if lead.phone:
        payload["phone"] = lead.phone
    if lead.location:
        payload["city"] = lead.location

    custom_values = {
        "lead_source": lead.source.value,
        "lead_score": qualification.qualification_score,
        "lead_priority": qualification.priority.value,
        "service_category": qualification.service_category,
        "ai_summary": qualification.summary,
        "missing_information": qualification.missing_information,
        "correlation_id": correlation_id,
        # The single most useful field during a support call: was this scored by
        # the model or by the fallback?
        "qualification_mode": "fallback" if qualification.degraded else qualification.provider,
    }
    fields = mapping.custom_field_payload(custom_values)
    if fields:
        payload["customFields"] = fields
    return payload


async def upsert_contact(
    client: GHLClient,
    lead: CanonicalLead,
    qualification: Qualification,
    routing: RoutingDecision,
    mapping: GHLMapping,
    *,
    correlation_id: str,
) -> ContactResult:
    """Create-or-update in one call.

    Upsert rather than search-then-create: the search-then-create pattern has a
    race (two forms submitted seconds apart both find nothing and both create)
    and GHL already resolves it server-side by email then phone within the
    location. Our own `leads` table is the second line of defence, not the first.
    """
    payload = build_contact_payload(
        lead, qualification, routing, mapping, correlation_id=correlation_id
    )
    response = await client.upsert_contact(payload)
    contact_id = _extract_contact_id(response.body)
    created = bool(response.body.get("new", response.status_code == 201))
    return ContactResult(contact_id=contact_id, created=created, raw=response.body)


async def attach_summary_note(
    client: GHLClient, contact_id: str, lead: CanonicalLead, qualification: Qualification
) -> GHLResponse:
    """Write the qualification onto the contact as a note.

    Custom fields are for filtering and automation; a note is what the person
    picking up the phone actually reads. Both, not either.
    """
    lines = [
        f"Lead summary ({qualification.provider}"
        + (" - fallback" if qualification.degraded else "")
        + ")",
        "",
        f"Intent: {qualification.intent}",
        f"Category: {qualification.service_category}",
        f"Priority: {qualification.priority.value}  |  "
        f"Score: {qualification.qualification_score}/100",
        "",
        qualification.summary,
        "",
        f"Recommended next step: {qualification.recommended_action}",
    ]
    if qualification.missing_information:
        lines += ["", "Missing before quoting: " + ", ".join(qualification.missing_information)]
    if lead.message:
        lines += ["", "--- original message ---", lead.message[:1000]]
    return await client.add_note(contact_id, "\n".join(lines))


async def ensure_opportunity(
    client: GHLClient,
    *,
    contact_id: str,
    lead: CanonicalLead,
    qualification: Qualification,
    routing: RoutingDecision,
    mapping: GHLMapping,
) -> tuple[str, bool]:
    """Return (opportunity_id, created).

    The duplicate-opportunity rule, which is the one that generates angry client
    messages: a returning lead with an *already open* opportunity gets that
    opportunity updated - moved stage, value refreshed - never a second card on
    the board. A new opportunity is only created when nothing is open.
    """
    stage_id = mapping.stage_id(routing.pipeline_stage)

    existing = await client.search_opportunities(
        contact_id=contact_id, pipeline_id=mapping.pipeline_id, status="open"
    )
    opportunities = existing.body.get("opportunities") or []
    if opportunities:
        opportunity_id = str(opportunities[0].get("id", ""))
        if opportunity_id:
            await client.update_opportunity(
                opportunity_id,
                {
                    "pipelineId": mapping.pipeline_id,
                    "pipelineStageId": stage_id,
                    "status": routing.opportunity_status,
                    "monetaryValue": routing.monetary_value,
                },
            )
            return opportunity_id, False

    name = f"{lead.full_name} - {qualification.service_category.replace('_', ' ')}"
    created = await client.create_opportunity(
        {
            "pipelineId": mapping.pipeline_id,
            "pipelineStageId": stage_id,
            "contactId": contact_id,
            "name": name[:200],
            "status": routing.opportunity_status,
            "monetaryValue": routing.monetary_value,
            "source": f"leadops:{lead.source.value}",
        }
    )
    return _extract_opportunity_id(created.body), True


async def send_follow_up(
    client: GHLClient,
    *,
    contact_id: str,
    lead: CanonicalLead,
    qualification: Qualification,
    routing: RoutingDecision,
) -> GHLResponse | None:
    """Send the first-touch message, if the routing decision calls for one.

    Channel choice is a business rule, not a technical one: SMS for anything with
    an SLA under an hour, email otherwise, and nothing at all when there is no
    sequence configured. Returning None rather than sending an empty message
    matters - a customer receiving a blank text is a support ticket.
    """
    if not routing.follow_up_sequence:
        return None

    readable_category = qualification.service_category.replace("_", " ")

    urgent = routing.sla_minutes is not None and routing.sla_minutes <= 60
    if urgent and lead.phone:
        channel, body = (
            "SMS",
            (
                f"Hi {lead.first_name or 'there'} - thanks for contacting us about "
                f"{readable_category}. "
                "We have your request and someone will call you shortly."
            ),
        )
    elif lead.email:
        channel, body = (
            "Email",
            (
                f"Hi {lead.first_name or 'there'},\n\n"
                "Thanks for getting in touch. We have received your enquiry"
                + (
                    f" and will be in touch about your {readable_category}."
                    if qualification.service_category != "other"
                    else "."
                )
                + (
                    "\n\nTo give you an accurate quote we still need: "
                    + ", ".join(qualification.missing_information)
                    + "."
                    if qualification.missing_information
                    else ""
                )
                + "\n\nBest regards"
            ),
        )
    elif lead.phone:
        channel, body = (
            "SMS",
            (
                f"Hi {lead.first_name or 'there'} - thanks for contacting us. "
                "We have received your enquiry and will be in touch."
            ),
        )
    else:
        return None

    payload: dict[str, Any] = {"type": channel, "contactId": contact_id, "message": body}
    if channel == "Email":
        payload["subject"] = "We received your enquiry"
        payload["html"] = body.replace("\n", "<br>")
    return await client.send_message(payload)


async def notify_sales(
    client: GHLClient,
    *,
    contact_id: str,
    lead: CanonicalLead,
    qualification: Qualification,
    routing: RoutingDecision,
    mapping: GHLMapping,
    notification_email: str,
) -> GHLResponse | None:
    """Internal alert for leads a human must touch inside an SLA.

    Implemented as an internal email through GHL rather than a separate Slack
    integration so the notification lives in the same system the salesperson is
    already in. `docs/architecture.md` notes how to swap this for Slack.
    """
    if not routing.notify_sales:
        return None

    sla = f"{routing.sla_minutes} minutes" if routing.sla_minutes else "today"
    body = (
        f"New {qualification.priority.value}-priority lead: {lead.full_name}\n"
        f"Category: {qualification.service_category} "
        f"(score {qualification.qualification_score}/100)\n"
        f"Contact: {lead.phone or 'no phone'} / {lead.email or 'no email'}\n"
        f"Area: {lead.location or 'not given'}\n"
        f"Respond within: {sla}\n"
        f"Routed by rule: {routing.rule_id} - {routing.reason}\n\n"
        f"{qualification.summary}\n\n"
        f"Original message: {lead.message[:400] or '(none)'}"
    )
    return await client.send_message(
        {
            "type": "Email",
            "contactId": contact_id,
            "subject": (
                f"[{qualification.priority.value.upper()}] {lead.full_name}"
                f" - {qualification.service_category}"
            ),
            "message": body,
            "html": body.replace("\n", "<br>"),
            "emailTo": notification_email,
            "userId": mapping.user_id(routing.notify_channel or "sales_primary"),
        }
    )
