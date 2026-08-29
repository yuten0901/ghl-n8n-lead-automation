"""Source adapters: raw provider payload -> `CanonicalLead`.

Each real-world source sends a different shape. Meta Lead Ads sends an array of
`field_data` question/answer pairs; Google Ads Lead Form sends `user_column_data`;
a website form sends whatever the form builder felt like. The adapters absorb that
so nothing downstream has to know which source a lead came from.

Adding a source = adding one function + one entry in SOURCE_ADAPTERS + fixtures.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from leadops.errors import ValidationFailed
from leadops.models import CanonicalLead, LeadSource
from leadops.normalize.fields import (
    clean_text,
    normalize_email,
    normalize_phone,
    parse_timestamp,
    split_name,
)

# Keys we recognise for the same concept across form builders.
_EMAIL_KEYS = ("email", "email_address", "e-mail", "emailaddress", "work_email")
_PHONE_KEYS = ("phone", "phone_number", "phonenumber", "tel", "telephone", "mobile")
_FIRST_KEYS = ("first_name", "firstname", "fname", "given_name")
_LAST_KEYS = ("last_name", "lastname", "lname", "family_name", "surname")
_NAME_KEYS = ("full_name", "fullname", "name", "your_name")
_SERVICE_KEYS = (
    "service",
    "service_requested",
    "service_type",
    "job_type",
    "interested_in",
    "product",
)
_MESSAGE_KEYS = (
    "message",
    "comments",
    "details",
    "notes",
    "description",
    "how_can_we_help",
    "enquiry",
)
_LOCATION_KEYS = (
    "location",
    "city",
    "suburb",
    "postcode",
    "zip",
    "zip_code",
    "address",
    "region",
)


def _first(payload: dict, keys: tuple[str, ...]) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in payload.items()}
    for key in keys:
        if lowered.get(key) not in (None, ""):
            return lowered[key]
    return ""


def _synthetic_external_id(source: str, payload: dict) -> str:
    """Sources that do not send an id still need a stable one, or every retry
    looks like a new lead. A content hash is stable for identical bodies."""
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return f"{source}_{digest}"


def _mapped_keys() -> set[str]:
    return {
        *_EMAIL_KEYS,
        *_PHONE_KEYS,
        *_FIRST_KEYS,
        *_LAST_KEYS,
        *_NAME_KEYS,
        *_SERVICE_KEYS,
        *_MESSAGE_KEYS,
        *_LOCATION_KEYS,
        "id",
        "lead_id",
        "leadgen_id",
        "submission_id",
        "submitted_at",
        "created_at",
        "created_time",
        "timestamp",
        "form_id",
        "campaign_id",
    }


def _leftovers(payload: dict) -> dict:
    """Unmapped-but-present fields. Kept rather than dropped, because the field a
    client actually cares about is usually in here on the first integration call."""
    known = _mapped_keys()
    return {
        str(k): v
        for k, v in payload.items()
        if str(k).strip().lower() not in known and v not in (None, "")
    }


def from_website(payload: dict) -> CanonicalLead:
    """Generic website / landing-page form post."""
    first = clean_text(_first(payload, _FIRST_KEYS), max_len=100)
    last = clean_text(_first(payload, _LAST_KEYS), max_len=100)
    if not first and not last:
        first, last = split_name(_first(payload, _NAME_KEYS))
    return CanonicalLead(
        external_id=clean_text(_first(payload, ("id", "lead_id", "submission_id")), max_len=120)
        or _synthetic_external_id("website", payload),
        source=LeadSource.WEBSITE,
        first_name=first,
        last_name=last,
        email=normalize_email(_first(payload, _EMAIL_KEYS)),
        phone=normalize_phone(_first(payload, _PHONE_KEYS)),
        service=clean_text(_first(payload, _SERVICE_KEYS), max_len=120),
        message=clean_text(_first(payload, _MESSAGE_KEYS)),
        location=clean_text(_first(payload, _LOCATION_KEYS), max_len=200),
        submitted_at=parse_timestamp(_first(payload, ("submitted_at", "created_at", "timestamp"))),
        extra=_leftovers(payload),
    )


def from_meta(payload: dict) -> CanonicalLead:
    """Meta (Facebook/Instagram) Lead Ads.

    Real shape: {"leadgen_id", "created_time", "form_id", "campaign_id",
                 "field_data": [{"name": "email", "values": ["a@b.com"]}, ...]}
    """
    flat: dict[str, Any] = {}
    for item in payload.get("field_data") or []:
        if not isinstance(item, dict):
            continue
        name = clean_text(item.get("name"), max_len=80).lower().replace(" ", "_")
        values = item.get("values") or []
        if name:
            flat[name] = values[0] if isinstance(values, list) and values else item.get("value", "")

    first = clean_text(_first(flat, _FIRST_KEYS), max_len=100)
    last = clean_text(_first(flat, _LAST_KEYS), max_len=100)
    if not first and not last:
        first, last = split_name(_first(flat, _NAME_KEYS))

    extra = _leftovers(flat)
    campaign = clean_text(payload.get("campaign_name") or payload.get("campaign_id"), max_len=120)
    if campaign:
        extra["campaign"] = campaign
    if payload.get("form_id"):
        extra["form_id"] = clean_text(payload["form_id"], max_len=120)

    return CanonicalLead(
        external_id=clean_text(payload.get("leadgen_id") or payload.get("id"), max_len=120)
        or _synthetic_external_id("meta", payload),
        source=LeadSource.META,
        first_name=first,
        last_name=last,
        email=normalize_email(_first(flat, _EMAIL_KEYS)),
        phone=normalize_phone(_first(flat, _PHONE_KEYS)),
        service=clean_text(_first(flat, _SERVICE_KEYS), max_len=120),
        message=clean_text(_first(flat, _MESSAGE_KEYS)),
        location=clean_text(_first(flat, _LOCATION_KEYS), max_len=200),
        submitted_at=parse_timestamp(payload.get("created_time") or payload.get("created_at")),
        extra=extra,
    )


def from_google(payload: dict) -> CanonicalLead:
    """Google Ads Lead Form extension webhook.

    Real shape: {"lead_id", "campaign_id", "gcl_id",
                 "user_column_data": [{"column_id": "EMAIL", "string_value": "..."}]}
    """
    flat: dict[str, Any] = {}
    for item in payload.get("user_column_data") or []:
        if not isinstance(item, dict):
            continue
        column = clean_text(item.get("column_id") or item.get("column_name"), max_len=80).lower()
        if column:
            flat[column] = item.get("string_value", "")

    email = _first(flat, ("email", "user_email"))
    phone = _first(flat, ("phone_number", "phone", "user_phone"))
    first = clean_text(flat.get("first_name", ""), max_len=100)
    last = clean_text(flat.get("last_name", ""), max_len=100)
    if not first and not last:
        first, last = split_name(flat.get("full_name", ""))

    consumed = {
        "email",
        "user_email",
        "phone_number",
        "phone",
        "user_phone",
        "first_name",
        "last_name",
        "full_name",
        "city",
        "postal_code",
    }
    extra = {k: v for k, v in flat.items() if k not in consumed and v}
    for key in ("campaign_id", "gcl_id", "form_id", "adgroup_id"):
        if payload.get(key):
            extra[key] = clean_text(payload[key], max_len=120)

    return CanonicalLead(
        external_id=clean_text(payload.get("lead_id") or payload.get("id"), max_len=120)
        or _synthetic_external_id("google", payload),
        source=LeadSource.GOOGLE,
        first_name=first,
        last_name=last,
        email=normalize_email(email),
        phone=normalize_phone(phone),
        service=clean_text(_first(flat, _SERVICE_KEYS), max_len=120),
        message=clean_text(_first(flat, _MESSAGE_KEYS)),
        location=clean_text(flat.get("city") or flat.get("postal_code"), max_len=200),
        submitted_at=parse_timestamp(payload.get("created_time") or payload.get("lead_created_at")),
        extra=extra,
    )


def from_partner(payload: dict) -> CanonicalLead:
    """A partner CRM/app posting an already-structured lead. Closest to the
    canonical shape, so this adapter is mostly validation."""
    lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else payload
    first = clean_text(_first(lead, _FIRST_KEYS), max_len=100)
    last = clean_text(_first(lead, _LAST_KEYS), max_len=100)
    if not first and not last:
        first, last = split_name(_first(lead, _NAME_KEYS))
    extra = _leftovers(lead)
    extra.pop("external_id", None)
    if payload.get("partner_id"):
        extra["partner_id"] = clean_text(payload["partner_id"], max_len=120)
    return CanonicalLead(
        external_id=clean_text(
            payload.get("event_id") or lead.get("external_id") or lead.get("id"), max_len=120
        )
        or _synthetic_external_id("partner", payload),
        source=LeadSource.PARTNER,
        first_name=first,
        last_name=last,
        email=normalize_email(_first(lead, _EMAIL_KEYS)),
        phone=normalize_phone(_first(lead, _PHONE_KEYS)),
        service=clean_text(_first(lead, _SERVICE_KEYS), max_len=120),
        message=clean_text(_first(lead, _MESSAGE_KEYS)),
        location=clean_text(_first(lead, _LOCATION_KEYS), max_len=200),
        submitted_at=parse_timestamp(_first(lead, ("submitted_at", "created_at", "timestamp"))),
        extra=extra,
    )


SOURCE_ADAPTERS: dict[str, Callable[[dict], CanonicalLead]] = {
    LeadSource.WEBSITE.value: from_website,
    LeadSource.META.value: from_meta,
    LeadSource.GOOGLE.value: from_google,
    LeadSource.PARTNER.value: from_partner,
}


def normalize_payload(source: str, payload: dict) -> CanonicalLead:
    """Normalize, then enforce the one rule that holds for every source:
    a lead we cannot contact is not a lead."""
    adapter = SOURCE_ADAPTERS.get(str(source).lower())
    if adapter is None:
        raise ValidationFailed(
            f"Unknown lead source '{source}'",
            detail={"known_sources": sorted(SOURCE_ADAPTERS)},
        )
    if not isinstance(payload, dict):
        raise ValidationFailed("Webhook body must be a JSON object")

    lead = adapter(payload)

    if not lead.has_contactable_identity():
        raise ValidationFailed(
            "Lead has neither an email address nor a phone number",
            detail={"source": lead.source.value, "external_id": lead.external_id},
        )
    return lead
