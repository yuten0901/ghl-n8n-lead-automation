"""Normalization: the layer that decides whether two submissions are one person."""

from __future__ import annotations

import pytest

from leadops.errors import ValidationFailed
from leadops.models import LeadSource
from leadops.normalize import normalize_payload
from leadops.normalize.fields import normalize_email, normalize_phone, parse_timestamp, split_name
from tests.conftest import load_fixture


class TestPhoneNormalization:
    @pytest.mark.parametrize(
        "raw",
        ["(512) 555-0147", "512-555-0147", "512.555.0147", "+1 512 555 0147", "5125550147"],
    )
    def test_all_us_formats_collapse_to_one_e164_value(self, raw: str) -> None:
        # This is the whole duplicate-contact problem in one assertion: five
        # spellings a customer might type, one value the CRM matches on.
        assert normalize_phone(raw) == "+15125550147"

    def test_unparseable_number_keeps_its_digits_rather_than_vanishing(self) -> None:
        # A lead with a mistyped phone is still a lead. Dropping the field would
        # silently downgrade the score for a data-entry slip.
        assert normalize_phone("555-01") == "55501"

    def test_empty_input_is_empty_output(self) -> None:
        assert normalize_phone("") == ""
        assert normalize_phone(None) == ""


class TestEmailNormalization:
    def test_case_and_whitespace_are_normalized(self) -> None:
        assert normalize_email("  Dana.Whitfield@Example.COM ") == "dana.whitfield@example.com"

    def test_plus_tags_and_dots_are_preserved(self) -> None:
        # Deliberate: stripping Gmail dots/+tags merges addresses that are
        # genuinely different for business domains. Over-merging is worse than
        # under-merging, because you cannot un-merge two customers.
        assert normalize_email("Sales+Leads@Example.com") == "sales+leads@example.com"
        assert normalize_email("first.last@example.com") == "first.last@example.com"

    @pytest.mark.parametrize("bad", ["not-an-email", "@example.com", "a@b", "a b@c.com", ""])
    def test_invalid_addresses_become_empty(self, bad: str) -> None:
        assert normalize_email(bad) == ""


class TestNameSplitting:
    @pytest.mark.parametrize(
        ("full", "expected"),
        [
            ("Dana Whitfield", ("Dana", "Whitfield")),
            ("Priya  Raghunathan", ("Priya", "Raghunathan")),
            ("Maria del Carmen Ruiz", ("Maria", "del Carmen Ruiz")),
            ("Cher", ("Cher", "")),
            ("", ("", "")),
        ],
    )
    def test_split(self, full: str, expected: tuple[str, str]) -> None:
        assert split_name(full) == expected


class TestTimestampParsing:
    @pytest.mark.parametrize(
        "raw", ["2026-08-29T07:14:22Z", "2026-08-29T07:14:22+00:00", 1787814862, 1787814862000]
    )
    def test_accepted_formats_are_utc_aware(self, raw: object) -> None:
        parsed = parse_timestamp(raw)
        assert parsed.tzinfo is not None

    def test_unreadable_timestamp_falls_back_to_now_instead_of_rejecting(self) -> None:
        # A malformed timestamp is not a reason to reject revenue.
        assert parse_timestamp("last tuesday").tzinfo is not None


class TestSourceAdapters:
    def test_website_form(self) -> None:
        lead = normalize_payload("website", load_fixture("website-lead-emergency.json"))
        assert lead.source is LeadSource.WEBSITE
        assert lead.first_name == "Dana"
        assert lead.email == "dana.whitfield@example.com"
        assert lead.phone == "+15125550147"
        assert "furnace stopped working" in lead.message

    def test_website_form_keeps_unmapped_fields(self) -> None:
        lead = normalize_payload("website", load_fixture("website-lead-emergency.json"))
        # utm_campaign is not part of the canonical schema, and it is exactly the
        # field a client asks for in week two. It must survive normalization.
        assert lead.extra["utm_campaign"] == "furnace-repair-aug"
        assert lead.extra["page_url"].startswith("https://")

    def test_meta_field_data_array_is_flattened(self) -> None:
        lead = normalize_payload("meta", load_fixture("meta-lead-ads.json"))
        assert lead.source is LeadSource.META
        assert lead.external_id == "meta_1099887766554433"
        assert lead.first_name == "Priya"
        assert lead.last_name == "Raghunathan"
        assert lead.email == "priya.r@example.net"
        assert lead.phone == "+15125550163"
        assert lead.location == "Cedar Park"
        assert lead.extra["campaign"] == "AC Install - Central TX - Aug"

    def test_google_user_column_data_is_flattened(self) -> None:
        lead = normalize_payload("google", load_fixture("google-lead-form.json"))
        assert lead.source is LeadSource.GOOGLE
        assert lead.external_id == "gads_77665544332211"
        assert lead.email == "t.herrera@example.com"
        assert lead.phone == "+15125550172"
        assert lead.location == "Pflugerville"
        assert lead.extra["gcl_id"].startswith("Cj0KCQ")

    def test_partner_nested_lead_object(self) -> None:
        lead = normalize_payload("partner", load_fixture("partner-lead.json"))
        assert lead.source is LeadSource.PARTNER
        assert lead.external_id == "partner_evt_20260829_0004"
        assert lead.service == "Warranty claim"
        assert lead.extra["partner_score"] == 82

    def test_all_four_sources_produce_the_same_shape(self) -> None:
        # The point of the adapter layer: downstream code never branches on source.
        leads = [
            normalize_payload("website", load_fixture("website-lead-standard.json")),
            normalize_payload("meta", load_fixture("meta-lead-ads.json")),
            normalize_payload("google", load_fixture("google-lead-form.json")),
            normalize_payload("partner", load_fixture("partner-lead.json")),
        ]
        shapes = {tuple(sorted(lead.model_dump().keys())) for lead in leads}
        assert len(shapes) == 1
        assert all(lead.has_contactable_identity() for lead in leads)


class TestRejection:
    def test_lead_with_no_email_and_no_phone_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            normalize_payload("website", load_fixture("invalid-lead-no-contact.json"))
        assert not excinfo.value.retryable  # redelivering will not add a phone number

    def test_unknown_source_is_rejected_and_lists_what_is_supported(self) -> None:
        with pytest.raises(ValidationFailed) as excinfo:
            normalize_payload("tiktok", {"email": "a@b.com"})
        assert "website" in excinfo.value.detail["known_sources"]

    def test_non_object_body_is_rejected(self) -> None:
        with pytest.raises(ValidationFailed):
            normalize_payload("website", ["not", "an", "object"])  # type: ignore[arg-type]

    def test_control_characters_are_stripped_from_free_text(self) -> None:
        lead = normalize_payload(
            "website",
            {"email": "a@b.com", "message": "line one\x07\x08 line two", "name": "A B"},
        )
        assert "\x07" not in lead.message
