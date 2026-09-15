"""Shared fixtures.

The important choice here: integration tests run the *real* pipeline against the
*real* mock GHL server, wired together in-process with an ASGI transport. No
network, no ports, no sleeping, but also no mocking of our own code - the code
under test is the code that ships. The only thing replaced is the vendor.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from mock.ghl_mock import server as mock_server

from leadops.config import Settings
from leadops.ghl.client import GHLClient
from leadops.ghl.mapping import GHLMapping
from leadops.reliability.retry import RetryPolicy
from leadops.routing.rules import RoutingTable
from leadops.storage import create_all, init_engine, session_scope
from leadops.storage.db import drop_all, reset_for_tests

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "n8n" / "fixtures"


def _database_url(tmp_path: Path) -> str:
    """SQLite by default; PostgreSQL when CI asks for it.

    The suite must pass on both, because SQLite is what a reviewer runs and
    PostgreSQL is what a real deployment uses. Differences between them - naive
    vs aware timestamps, DEFERRED vs IMMEDIATE transactions - are exactly the
    kind of thing that only shows up when something runs on both.

    On PostgreSQL the `db` fixture drops and recreates the schema per test,
    which gives the same isolation a per-test SQLite file gives.
    """
    configured = os.environ.get("LEADOPS_TEST_DATABASE_URL", "").strip()
    if configured:
        return configured
    return f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite3'}"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="local",
        database_url=_database_url(tmp_path),
        ghl_base_url="http://ghl.mock",
        ghl_access_token="test_token_not_a_real_secret",
        ghl_location_id="loc_TEST000000000000000",
        ghl_pipeline_id="pipe_TEST00000000000000",
        ai_provider="deterministic",
        ghl_max_attempts=3,
        ghl_backoff_base_seconds=0.0,
        ghl_backoff_max_seconds=0.0,
        require_signature=False,
        webhook_signing_secret="",
        max_delivery_attempts=3,
        routing_config_path=str(REPO_ROOT / "config" / "routing.yml"),
        ghl_mapping_path=str(REPO_ROOT / "config" / "ghl-mapping.json"),
    )


@pytest.fixture
async def db(settings: Settings):
    """A clean schema per test, whichever backend is in play."""
    await reset_for_tests()
    init_engine(settings.database_url)
    if not settings.database_url.startswith("sqlite"):
        # A shared PostgreSQL service has no per-test file to throw away.
        await drop_all()
    await create_all()
    yield
    if not settings.database_url.startswith("sqlite"):
        await drop_all()
    await reset_for_tests()


@pytest.fixture
def mock_ghl():
    """The in-process mock, reset between tests so faults never leak."""
    mock_server.state.reset()
    yield mock_server.state
    mock_server.state.reset()


@pytest.fixture
def ghl_client(settings: Settings, mock_ghl) -> GHLClient:
    """A real GHLClient whose transport is the mock app.

    Retry delays are zeroed rather than patched away, so the retry *logic* still
    runs - the test proves three attempts happened, it does not skip them.
    """
    transport = httpx.ASGITransport(app=mock_server.app)
    http = httpx.AsyncClient(transport=transport, base_url="http://ghl.mock", timeout=5.0)
    return GHLClient(
        base_url="http://ghl.mock",
        access_token=settings.ghl_access_token,
        location_id=settings.ghl_location_id,
        api_version="v3",
        client=http,
        policy=RetryPolicy(max_attempts=3, base_seconds=0.0, max_seconds=0.0),
    )


@pytest.fixture
def mapping(settings: Settings) -> GHLMapping:
    loaded = GHLMapping.load(settings.ghl_mapping_path)
    loaded.location_id = settings.ghl_location_id
    loaded.pipeline_id = settings.ghl_pipeline_id
    return loaded


@pytest.fixture
def table(settings: Settings) -> RoutingTable:
    return RoutingTable.load(settings.routing_config_path)


@pytest.fixture
async def session():
    async with session_scope() as s:
        yield s


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def fixture_loader():
    return load_fixture


async def set_fault(operation: str, mode: str, times: int = 1) -> None:
    """Schedule a fault on the mock, through its HTTP control plane rather than
    by poking at module state - the same way the demo scripts do it."""
    transport = httpx.ASGITransport(app=mock_server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ghl.mock") as client:
        await client.post(
            "/_mock/faults",
            json={"operation": operation, "mode": mode, "times": times},
            headers={"Version": "v3", "Authorization": "Bearer test_token_not_a_real"},
        )
