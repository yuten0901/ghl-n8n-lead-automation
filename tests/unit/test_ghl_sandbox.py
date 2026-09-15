"""Sandbox検証の安全装置と公開証跡を検査する。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from mock.ghl_mock import server as mock_server

from leadops.errors import UpstreamRejected, ValidationFailed
from leadops.ghl.client import GHLClient
from leadops.ghl.sandbox import _optional_read, authorize_sandbox_write, verify_sandbox
from leadops.reliability.retry import RetryPolicy


def client() -> GHLClient:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_server.app),
        base_url="http://ghl.mock",
        timeout=5,
    )
    return GHLClient(
        base_url="http://ghl.mock",
        access_token="test_token_not_a_real_secret",
        location_id="loc_TEST000000000000000",
        api_version="v3",
        client=http,
        policy=RetryPolicy(max_attempts=1),
    )


def test_write_requires_both_guard_and_exact_location() -> None:
    with pytest.raises(ValidationFailed):
        authorize_sandbox_write(sandbox_guard="false", confirmation="loc_1", location_id="loc_1")
    with pytest.raises(ValidationFailed):
        authorize_sandbox_write(sandbox_guard="true", confirmation="loc_wrong", location_id="loc_1")


@pytest.mark.parametrize("status_code", [401, 403])
async def test_optional_read_treats_missing_private_integration_scope_as_optional(
    status_code: int,
) -> None:
    ghl = AsyncMock()
    ghl.request.side_effect = UpstreamRejected(
        "scope unavailable", detail={"status_code": status_code}
    )

    result = await _optional_read(ghl, "/optional", params={"locationId": "loc_1"})

    assert result == {"_scope_available": False}


async def test_read_only_preflight_does_not_write(mock_ghl) -> None:
    ghl = client()
    try:
        evidence = await verify_sandbox(ghl)
    finally:
        await ghl.aclose()
    assert evidence.mode == "read-only"
    assert evidence.checks["authentication_and_read_scopes"] is True
    assert mock_ghl.contacts == {}
    assert mock_ghl.opportunities == {}


async def test_write_check_proves_contact_and_opportunity_reuse(mock_ghl) -> None:
    ghl = client()
    try:
        evidence = await verify_sandbox(
            ghl,
            write_test=True,
            sandbox_guard="true",
            confirmation=ghl.location_id,
            pipeline_id="pipe_TEST00000000000000",
            stage_id="stage_TEST_new_lead_0000",
            marker="fixed-test-marker",
        )
    finally:
        await ghl.aclose()
    assert evidence.checks["contact_upsert_is_idempotent"] is True
    assert evidence.checks["opportunity_is_reused"] is True
    assert len(mock_ghl.contacts) == 1
    assert len(mock_ghl.opportunities) == 1


async def test_public_evidence_contains_no_credentials_or_test_email(mock_ghl) -> None:
    ghl = client()
    try:
        evidence = await verify_sandbox(
            ghl,
            write_test=True,
            sandbox_guard="true",
            confirmation=ghl.location_id,
            pipeline_id="pipe_TEST00000000000000",
            stage_id="stage_TEST_new_lead_0000",
            marker="private-marker",
        )
    finally:
        await ghl.aclose()
    rendered = str(evidence.as_dict())
    assert ghl.access_token not in rendered
    assert "@example.com" not in rendered
    assert "private-marker" not in rendered
    assert ghl.location_id not in rendered
