"""No database transaction may stay open across a network call.

This is the property `pipeline._commit()` claims, and for a while the code did
not have it. `storage/db.py` makes every SQLite transaction `BEGIN IMMEDIATE` -
which is what stops writers deadlocking - but that also means a plain *read*
takes the database's write lock. `_step()` began with a `read_step()` lookup and
did not commit before calling GoHighLevel, so the write lock was held for the
whole vendor round trip.

Measured on the unfixed code: a three-second call blocked a competing write for
2.74 seconds, and a seven-second call failed it outright with "database is
locked". GoHighLevel calls retry up to four times with up to eight seconds of
backoff, so "slower than five seconds" is ordinary operation rather than an edge
case - the failure was reachable in normal use.

Every test here is written so that it fails on the unfixed code. A test that
passes either way would not have caught this and will not catch the next one.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from leadops.storage import repo, session_scope

pytestmark = pytest.mark.usefixtures("db")

# Long enough that a held lock is unmistakable, short enough to keep the suite
# fast. SQLite's busy_timeout is 5s, so this stays inside "blocks" rather than
# "fails", which is the weaker and therefore safer thing to assert.
SLOW_CALL_SECONDS = 1.5
BLOCKED_THRESHOLD = 0.5


async def _competing_write(barrier: asyncio.Event) -> float:
    """Wait until the reader is mid-'network call', then time a write."""
    await barrier.wait()
    started = time.perf_counter()
    async with session_scope() as session:
        await repo.write_step(
            session, step_key="competitor#step", event_id="evt_other", step="step", output={}
        )
    return time.perf_counter() - started


async def test_a_step_lookup_does_not_hold_the_write_lock_across_the_call() -> None:
    """The shape of `_step`: read the memo, then call out.

    Reproduced directly rather than through the pipeline so the assertion is
    about the transaction boundary and nothing else.
    """
    barrier = asyncio.Event()

    async def reader() -> None:
        async with session_scope() as session:
            await repo.read_step(session, "some_event#ghl_contact")
            # The line under test. Without it the lock below is held for the
            # whole of SLOW_CALL_SECONDS.
            await session.commit()
            barrier.set()
            await asyncio.sleep(SLOW_CALL_SECONDS)

    _, blocked_for = await asyncio.gather(reader(), _competing_write(barrier))

    assert blocked_for < BLOCKED_THRESHOLD, (
        f"a competing write waited {blocked_for:.2f}s while a step was calling out; "
        "the read transaction is still open across the network call"
    )


async def test_the_probe_itself_detects_a_held_lock() -> None:
    """Negative control.

    The test above asserts that something does *not* happen, which is the kind
    of test that passes for the wrong reason - if the timing never blocked
    anything, it would be green on broken code too. This one omits the commit
    and requires the block to appear, so the measurement is known to work.
    """
    barrier = asyncio.Event()

    async def reader_without_commit() -> None:
        async with session_scope() as session:
            await repo.read_step(session, "some_event#ghl_contact")
            barrier.set()
            await asyncio.sleep(SLOW_CALL_SECONDS)

    _, blocked_for = await asyncio.gather(reader_without_commit(), _competing_write(barrier))

    assert blocked_for >= BLOCKED_THRESHOLD, (
        f"the competing write only waited {blocked_for:.2f}s with the commit removed, "
        "so this measurement cannot distinguish a held lock from a released one"
    )


async def test_the_pipeline_commits_before_every_outbound_call(
    settings, ghl_client, mapping, table, mock_ghl
) -> None:
    """End-to-end version: while a real lead is mid-flight in the GoHighLevel
    client, an unrelated write must still go through."""
    from leadops.pipeline import process_lead
    from tests.conftest import load_fixture

    blocked_for: list[float] = []
    original_upsert = type(ghl_client).upsert_contact

    async def slow_upsert(self, payload):  # noqa: ANN001, ANN202
        started = time.perf_counter()

        async def competitor() -> None:
            async with session_scope() as session:
                await repo.write_step(
                    session,
                    step_key="unrelated#work",
                    event_id="evt_unrelated",
                    step="work",
                    output={},
                )

        await asyncio.gather(asyncio.sleep(SLOW_CALL_SECONDS), competitor())
        blocked_for.append(time.perf_counter() - started - SLOW_CALL_SECONDS)
        return await original_upsert(self, payload)

    type(ghl_client).upsert_contact = slow_upsert
    try:
        async with session_scope() as session:
            await process_lead(
                source="website",
                payload=load_fixture("website-lead-standard.json"),
                headers={},
                settings=settings,
                session=session,
                ghl=ghl_client,
                mapping=mapping,
                table=table,
            )
    finally:
        type(ghl_client).upsert_contact = original_upsert

    assert blocked_for, "the slow upsert never ran"
    assert max(blocked_for) < BLOCKED_THRESHOLD, (
        f"an unrelated write waited {max(blocked_for):.2f}s beyond the vendor call; "
        "the pipeline is holding a transaction open across it"
    )
