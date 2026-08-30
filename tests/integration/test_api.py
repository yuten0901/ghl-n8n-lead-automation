"""HTTP surface: status codes, signatures, and the admin views.

The status-code assertions matter more than they look. A webhook sender decides
whether to redeliver from the status code, so returning 200 for a retryable
failure silently drops leads, and returning 500 for a malformed payload makes the
sender hammer you forever with a body that will never work.
"""

from __future__ import annotations

import json

import httpx
import pytest
from mock.ghl_mock import server as mock_server

from leadops.api.main import create_app
from leadops.api.security import sign
from leadops.storage import create_all, init_engine
from leadops.storage.db import drop_all, reset_for_tests
from tests.conftest import load_fixture

SECRET = "whsec_test_value_not_a_real_secret"


@pytest.fixture
async def client(settings, mock_ghl, monkeypatch):
    """The real app, with GHL calls routed to the in-process mock.

    Only the vendor transport is redirected. Routing, storage, idempotency and
    the pipeline are the shipped code.
    """
    await reset_for_tests()
    init_engine(settings.database_url)
    # drop before create: on SQLite each test gets its own tmp_path file, so
    # isolation was accidental rather than designed. PostgreSQL shares one
    # database across the suite, and without this the rows from the previous
    # test are still there - which showed up as "2 dead letters, expected 1",
    # the second one produced when claim_event legitimately reclaimed the
    # dead-lettered event left behind by an earlier test.
    await drop_all()
    await create_all()

    import leadops.pipeline as pipeline_module
    from leadops.ghl.client import GHLClient
    from leadops.reliability.retry import RetryPolicy

    real_client_cls = GHLClient

    def build_mock_backed_client(**kwargs):
        kwargs["client"] = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mock_server.app),
            base_url="http://ghl.mock",
            timeout=5.0,
        )
        kwargs["policy"] = RetryPolicy(max_attempts=2, base_seconds=0.0, max_seconds=0.0)
        return real_client_cls(**kwargs)

    monkeypatch.setattr(pipeline_module, "GHLClient", build_mock_backed_client)

    app = create_app(settings)
    app.state.mapping = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
    await reset_for_tests()


async def post_lead(client: httpx.AsyncClient, source: str, fixture: str, **kwargs):
    body = json.dumps(load_fixture(fixture)).encode()
    return await client.post(
        f"/webhooks/leads/{source}",
        content=body,
        headers={"Content-Type": "application/json", **kwargs.pop("headers", {})},
        **kwargs,
    )


class TestHealth:
    async def test_healthz_reports_configuration_without_leaking_it(self, client) -> None:
        response = await client.get("/healthz")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert sorted(payload["sources"]) == ["google", "meta", "partner", "website"]
        # It says *whether* a token is configured, never what it is.
        assert payload["config"]["ghl_token_configured"] is True
        assert "ghl_access_token" not in json.dumps(payload)
        assert "test_token_not_a_real_secret" not in json.dumps(payload)


class TestStatusCodes:
    async def test_a_good_lead_returns_200_with_the_routing_decision(self, client) -> None:
        response = await post_lead(client, "website", "website-lead-emergency.json")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "succeeded"
        assert body["contact_id"] and body["opportunity_id"]
        assert body["routing"]["rule_id"] == "emergency_high_priority"
        assert body["qualification"]["priority"] == "high"
        assert body["correlation_id"].startswith("cid_")

    async def test_a_duplicate_returns_200_and_says_so(self, client) -> None:
        await post_lead(client, "meta", "meta-lead-ads.json")
        second = await post_lead(client, "meta", "meta-lead-ads.json")
        assert second.status_code == 200
        assert second.json()["status"] == "duplicate"

    async def test_an_uncontactable_lead_returns_400_not_500(self, client) -> None:
        """400 tells the sender to stop redelivering. A 500 would make it retry a
        payload that can never succeed."""
        response = await post_lead(client, "website", "invalid-lead-no-contact.json")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_failed"
        assert response.json()["error"]["retryable"] is False

    async def test_malformed_json_returns_400(self, client) -> None:
        response = await client.post(
            "/webhooks/leads/website",
            content=b"{not json at all",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "malformed_json"

    async def test_a_json_array_body_returns_400(self, client) -> None:
        response = await client.post(
            "/webhooks/leads/website",
            content=b'[{"email":"a@b.com"}]',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

    async def test_an_unknown_source_returns_400_listing_the_known_ones(self, client) -> None:
        response = await client.post(
            "/webhooks/leads/tiktok",
            content=b'{"email":"a@b.com"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert "website" in response.json()["error"]["detail"]["known_sources"]

    async def test_a_retryable_failure_returns_202_so_the_sender_redelivers(
        self, client, mock_ghl
    ) -> None:
        mock_ghl.faults["contacts.upsert"] = mock_server.Fault(mode="500", remaining=10)
        response = await post_lead(client, "website", "website-lead-standard.json")
        assert response.status_code == 202
        assert response.json()["error"]["retryable"] is True

    async def test_a_permanent_failure_returns_422_so_the_sender_stops(
        self, client, mock_ghl
    ) -> None:
        mock_ghl.faults["contacts.upsert"] = mock_server.Fault(mode="401", remaining=1)
        response = await post_lead(client, "website", "website-lead-standard.json")
        assert response.status_code == 422
        assert response.json()["status"] == "dead_lettered"


class TestSignatures:
    @pytest.fixture
    async def signed_client(self, settings, mock_ghl, monkeypatch):
        settings.webhook_signing_secret = SECRET
        settings.require_signature = True
        await reset_for_tests()
        init_engine(settings.database_url)
        await drop_all()
        await create_all()
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
        await reset_for_tests()

    async def test_a_request_with_no_signature_is_rejected_401(self, signed_client) -> None:
        response = await signed_client.post(
            "/webhooks/leads/website", content=b'{"email":"a@b.com"}'
        )
        assert response.status_code == 401

    async def test_a_request_with_a_bad_signature_is_rejected_401(self, signed_client) -> None:
        body = b'{"email":"a@b.com","phone":"5125550101","message":"hello there"}'
        response = await signed_client.post(
            "/webhooks/leads/website",
            content=body,
            headers={"X-Signature": sign(body, "the_wrong_secret")},
        )
        assert response.status_code == 401

    async def test_a_correctly_signed_request_is_accepted_and_marked_verified(
        self, signed_client
    ) -> None:
        body = json.dumps(load_fixture("website-lead-standard.json")).encode()
        response = await signed_client.post(
            "/webhooks/leads/website",
            content=body,
            headers={"X-Signature": sign(body, SECRET), "Content-Type": "application/json"},
        )
        assert response.status_code in (200, 202, 422)
        assert response.json()["signature_verified"] is True

    async def test_an_unsigned_deployment_reports_signature_verified_false(self, client) -> None:
        """Not an error - but the response never claims a verification that did
        not happen, and /admin/stats counts it."""
        response = await post_lead(client, "website", "website-lead-standard.json")
        assert response.json()["signature_verified"] is False


class TestIdempotencyHeader:
    async def test_the_idempotency_header_deduplicates_bodies_that_differ(
        self, client, mock_ghl
    ) -> None:
        """A sender that retries with a regenerated timestamp still deduplicates,
        because the header is authoritative over the body hash."""
        first_body = json.dumps({**load_fixture("website-lead-standard.json"), "attempt": 1})
        second_body = json.dumps({**load_fixture("website-lead-standard.json"), "attempt": 2})
        headers = {"Content-Type": "application/json", "X-Idempotency-Key": "delivery-abc-123"}

        first = await client.post("/webhooks/leads/website", content=first_body, headers=headers)
        second = await client.post("/webhooks/leads/website", content=second_body, headers=headers)

        assert first.json()["status"] == "succeeded"
        assert second.json()["status"] == "duplicate"
        assert len(mock_ghl.contacts) == 1


class TestAdminViews:
    async def test_event_detail_explains_the_whole_lead_journey(self, client) -> None:
        posted = (await post_lead(client, "website", "website-lead-emergency.json")).json()
        response = await client.get(f"/admin/events/{posted['event_id']}")
        assert response.status_code == 200
        detail = response.json()

        steps = [s["step"] for s in detail["steps"]]
        assert "ai_qualification" in steps and "ghl_contact" in steps
        assert all("duration_ms" in s for s in detail["steps"])

        providers = {c["provider"] for c in detail["provider_calls"]}
        assert "ghl" in providers
        assert detail["result"]["routing"]["reason"]  # the human-readable "why"

    async def test_a_missing_event_returns_404(self, client) -> None:
        assert (await client.get("/admin/events/evt_does_not_exist")).status_code == 404

    async def test_events_can_be_filtered_by_status(self, client) -> None:
        await post_lead(client, "website", "website-lead-standard.json")
        response = await client.get("/admin/events", params={"status": "succeeded"})
        assert response.status_code == 200
        assert all(e["status"] == "succeeded" for e in response.json()["events"])

    async def test_stats_surface_weak_keys_and_unverified_signatures(self, client) -> None:
        await post_lead(client, "website", "website-lead-standard.json")
        stats = (await client.get("/admin/stats")).json()
        assert stats["sampled_events"] >= 1
        assert "by_status" in stats
        assert stats["unverified_signatures"] >= 1  # no secret configured in this fixture

    async def test_a_dead_lettered_event_is_listed_with_its_error(self, client, mock_ghl) -> None:
        mock_ghl.faults["contacts.upsert"] = mock_server.Fault(mode="401", remaining=1)
        await post_lead(client, "website", "website-lead-standard.json")
        entries = (await client.get("/admin/dead-letters")).json()
        assert entries["count"] == 1
        assert entries["dead_letters"][0]["reason"] == "permanent"


class TestReplay:
    async def test_replaying_a_dead_letter_after_the_fault_clears_completes_the_lead(
        self, client, mock_ghl
    ) -> None:
        """The operational story: a credential was wrong, someone fixed it, and
        the queued leads are recovered without re-asking the customer."""
        mock_ghl.faults["contacts.upsert"] = mock_server.Fault(mode="401", remaining=1)
        await post_lead(client, "website", "website-lead-standard.json")

        listed = (await client.get("/admin/dead-letters")).json()["dead_letters"]
        assert len(listed) == 1
        mock_ghl.faults.clear()  # the credential is fixed

        replayed = await client.post(f"/admin/dead-letters/{listed[0]['id']}/replay")
        assert replayed.status_code == 200
        assert replayed.json()["status"] == "succeeded"
        assert len(mock_ghl.contacts) == 1
        assert len(mock_ghl.opportunities) == 1

        remaining = (await client.get("/admin/dead-letters")).json()
        assert remaining["count"] == 0  # resolved, not left lying around

    async def test_replaying_an_unknown_dead_letter_returns_404(self, client) -> None:
        assert (await client.post("/admin/dead-letters/99999/replay")).status_code == 404
