"""Idempotency key derivation and the two-key distinction."""

from __future__ import annotations

from datetime import UTC, datetime

from leadops.idempotency import event_idempotency_key, is_weak, lead_identity_key, step_key
from leadops.models import CanonicalLead, LeadSource
from leadops.normalize import normalize_payload
from tests.conftest import load_fixture


def _lead(**overrides) -> CanonicalLead:
    base = {
        "external_id": "x1",
        "source": LeadSource.WEBSITE,
        "email": "dana@example.com",
        "phone": "+15125550147",
        "submitted_at": datetime(2026, 8, 29, tzinfo=UTC),
    }
    return CanonicalLead(**{**base, **overrides})


class TestEventKeyDerivation:
    def test_explicit_key_beats_everything_else(self) -> None:
        key, derivation = event_idempotency_key(
            source="website",
            payload={"id": "body-id"},
            headers={"X-Request-Id": "header-id"},
            explicit_key="caller-supplied",
        )
        assert key == "website:caller-supplied"
        assert derivation == "explicit"

    def test_delivery_header_beats_body_id(self) -> None:
        _, derivation = event_idempotency_key(
            source="meta", payload={"leadgen_id": "lg1"}, headers={"X-Webhook-Id": "wh1"}
        )
        assert derivation == "header:x-webhook-id"

    def test_header_matching_is_case_insensitive(self) -> None:
        # Header casing varies by proxy; treating "X-Request-ID" and
        # "x-request-id" as different keys would break deduplication silently.
        lower = event_idempotency_key(source="w", payload={}, headers={"x-request-id": "r1"})
        upper = event_idempotency_key(source="w", payload={}, headers={"X-Request-ID": "r1"})
        assert lower == upper

    def test_body_id_is_used_when_no_header_is_present(self) -> None:
        key, derivation = event_idempotency_key(source="meta", payload={"leadgen_id": "lg99"})
        assert key == "meta:lg99"
        assert derivation == "body:leadgen_id"

    def test_body_hash_is_the_last_resort_and_is_reported_as_weak(self) -> None:
        _, derivation = event_idempotency_key(
            source="website", payload={"email": "a@b.com", "message": "hi"}
        )
        assert derivation == "body_hash"
        assert is_weak(derivation)

    def test_body_hash_is_stable_regardless_of_key_order(self) -> None:
        # A sender that serialises keys in a different order on redelivery must
        # still deduplicate. Sorting the keys before hashing is what guarantees it.
        first = event_idempotency_key(source="w", payload={"a": 1, "b": 2})
        second = event_idempotency_key(source="w", payload={"b": 2, "a": 1})
        assert first == second

    def test_different_bodies_produce_different_keys(self) -> None:
        first = event_idempotency_key(source="w", payload={"message": "quote please"})
        second = event_idempotency_key(source="w", payload={"message": "quote please!"})
        assert first[0] != second[0]

    def test_the_same_id_from_different_sources_does_not_collide(self) -> None:
        # Two providers can both call their event "1". Scoping by source is why
        # a Meta lead cannot suppress a Google lead.
        meta = event_idempotency_key(source="meta", payload={"lead_id": "1"})
        google = event_idempotency_key(source="google", payload={"lead_id": "1"})
        assert meta[0] != google[0]

    def test_real_fixtures_derive_keys_from_their_provider_ids(self) -> None:
        meta_key, meta_deriv = event_idempotency_key(
            source="meta", payload=load_fixture("meta-lead-ads.json")
        )
        google_key, google_deriv = event_idempotency_key(
            source="google", payload=load_fixture("google-lead-form.json")
        )
        assert meta_key == "meta:meta_1099887766554433"
        assert google_key == "google:gads_77665544332211"
        assert not is_weak(meta_deriv) and not is_weak(google_deriv)


class TestLeadIdentityKey:
    def test_email_is_the_primary_identifier(self) -> None:
        key = lead_identity_key(_lead(), location_id="loc1")
        assert key == "loc1:email:dana@example.com"

    def test_phone_is_used_when_there_is_no_email(self) -> None:
        key = lead_identity_key(_lead(email=""), location_id="loc1")
        assert key == "loc1:phone:+15125550147"

    def test_same_person_from_two_sources_gets_one_identity(self) -> None:
        # The actual business case: a homeowner fills the website form on Monday
        # and the Google Ads form on Friday. Two events, one CRM contact.
        website = _lead(source=LeadSource.WEBSITE, external_id="ws1")
        google = _lead(source=LeadSource.GOOGLE, external_id="g1", phone="")
        assert lead_identity_key(website, location_id="loc1") == lead_identity_key(
            google, location_id="loc1"
        )

    def test_identity_is_scoped_to_the_location(self) -> None:
        # Multi-tenant safety: two agencies' contacts must never merge.
        assert lead_identity_key(_lead(), location_id="locA") != lead_identity_key(
            _lead(), location_id="locB"
        )

    def test_phone_formatting_differences_do_not_split_one_person(self) -> None:
        typed_one_way = normalize_payload(
            "website", {"phone": "(512) 555-0147", "name": "Dana W", "message": "hi"}
        )
        typed_another_way = normalize_payload(
            "website", {"phone": "+1 512 555 0147", "name": "Dana W", "message": "hi"}
        )
        assert lead_identity_key(typed_one_way, location_id="loc1") == lead_identity_key(
            typed_another_way, location_id="loc1"
        )


class TestStepKey:
    def test_step_keys_are_scoped_to_one_event(self) -> None:
        assert step_key("website:e1", "ghl_contact") != step_key("website:e2", "ghl_contact")

    def test_step_keys_are_scoped_to_one_step(self) -> None:
        assert step_key("website:e1", "ghl_contact") != step_key("website:e1", "ghl_opportunity")
