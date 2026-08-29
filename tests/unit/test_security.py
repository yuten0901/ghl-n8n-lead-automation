"""Webhook signature verification and log redaction."""

from __future__ import annotations

import json
import logging
import time

import pytest

from leadops.api.security import sign, verify
from leadops.errors import SignatureInvalid
from leadops.logging_setup import JsonFormatter, redact

SECRET = "whsec_test_value_not_a_real_secret"


class TestSignatureVerification:
    def test_a_correctly_signed_body_verifies(self) -> None:
        body = b'{"email":"a@b.com"}'
        assert verify(body, sign(body, SECRET), SECRET).verified is True

    def test_a_tampered_body_does_not_verify(self) -> None:
        body = b'{"email":"a@b.com"}'
        header = sign(body, SECRET)
        with pytest.raises(SignatureInvalid):
            verify(b'{"email":"attacker@evil.test"}', header, SECRET)

    def test_the_wrong_secret_does_not_verify(self) -> None:
        body = b'{"a":1}'
        with pytest.raises(SignatureInvalid):
            verify(body, sign(body, SECRET), "whsec_a_different_secret")

    def test_a_captured_old_request_cannot_be_replayed(self) -> None:
        """Signing only the body would make a captured request valid forever.

        The signature covers `{timestamp}.{body}` and old timestamps are refused.
        """
        body = b'{"a":1}'
        old_header = sign(body, SECRET, timestamp=int(time.time()) - 3600)
        with pytest.raises(SignatureInvalid) as excinfo:
            verify(body, old_header, SECRET, max_skew_seconds=300)
        assert "window" in excinfo.value.message

    def test_a_future_timestamp_is_also_refused(self) -> None:
        body = b'{"a":1}'
        future = sign(body, SECRET, timestamp=int(time.time()) + 3600)
        with pytest.raises(SignatureInvalid):
            verify(body, future, SECRET, max_skew_seconds=300)

    def test_a_timestamp_inside_the_window_is_accepted(self) -> None:
        body = b'{"a":1}'
        header = sign(body, SECRET, timestamp=int(time.time()) - 60)
        assert verify(body, header, SECRET, max_skew_seconds=300).verified is True

    @pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=def", "v1=onlysignature", "t=123"])
    def test_malformed_headers_are_refused(self, header: str) -> None:
        with pytest.raises(SignatureInvalid):
            verify(b"{}", header, SECRET)

    def test_missing_header_is_refused_when_signatures_are_required(self) -> None:
        with pytest.raises(SignatureInvalid):
            verify(b"{}", None, SECRET, required=True)

    def test_byte_exact_body_matters(self) -> None:
        """Re-serialising before verifying breaks on key order and whitespace.

        This is why the route signs raw bytes and never `json.dumps(parsed)`.
        """
        original = b'{"b":2,"a":1}'
        header = sign(original, SECRET)
        reserialised = json.dumps(json.loads(original)).encode()
        assert reserialised != original
        with pytest.raises(SignatureInvalid):
            verify(reserialised, header, SECRET)


class TestUnconfiguredSecret:
    def test_no_secret_reports_unverified_rather_than_claiming_success(self) -> None:
        """The failure mode this prevents: a deployment that believes it is
        authenticating webhooks when nothing is configured."""
        check = verify(b"{}", "t=1,v1=x", "", required=False)
        assert check.verified is False
        assert check.reason == "no_secret_configured"

    def test_no_secret_with_signatures_required_is_a_hard_failure(self) -> None:
        with pytest.raises(SignatureInvalid):
            verify(b"{}", "t=1,v1=x", "", required=True)


class TestLogRedaction:
    @pytest.mark.parametrize(
        "text",
        [
            "contacting dana.whitfield@example.com about the job",
            "phone +1 512 555 0147 on file",
            "called 512-555-0147 twice",
        ],
    )
    def test_pii_is_masked_in_strings(self, text: str) -> None:
        masked = redact(text)
        assert "@example.com" not in masked
        assert "555-0147" not in masked
        assert "5550147" not in masked.replace(" ", "")

    def test_redaction_reaches_into_nested_structures(self) -> None:
        payload = {"lead": {"contacts": [{"email": "a@b.com"}]}, "note": "call +15125550147"}
        masked = redact(payload)
        assert masked["lead"]["contacts"][0]["email"] == "[email]"
        assert "[phone]" in masked["note"]

    def test_the_json_formatter_redacts_extra_fields(self) -> None:
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="event.succeeded",
            args=(),
            exc_info=None,
        )
        record.customer_email = "dana@example.com"  # type: ignore[attr-defined]
        emitted = json.loads(JsonFormatter().format(record))
        assert emitted["customer_email"] == "[email]"
        assert emitted["event"] == "event.succeeded"
        assert emitted["level"] == "INFO"

    def test_non_pii_values_survive_unchanged(self) -> None:
        assert redact({"score": 92, "stage": "hot_lead"}) == {"score": 92, "stage": "hot_lead"}

    def test_correlation_ids_are_not_mistaken_for_phone_numbers(self) -> None:
        """Regression: a looser phone pattern masked the middle of correlation ids,
        destroying the one field the logs exist to make searchable."""
        for identifier in [
            "cid_2b6e415512340a9f",
            "evt_759a875a73e44e37815e",
            "n8n_4155501234567_1756468800000",
            "loc_DEMO0000000000000000",
        ]:
            assert redact(identifier) == identifier

    def test_a_real_phone_number_is_still_masked_in_the_same_sentence(self) -> None:
        line = "cid_2b6e415512340a9f called +1 512 555 0147"
        masked = redact(line)
        assert "cid_2b6e415512340a9f" in masked
        assert "[phone]" in masked
