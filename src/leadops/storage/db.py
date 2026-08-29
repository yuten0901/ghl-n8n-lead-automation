"""Engine and session management.

**Async, deliberately.** The service is async because it spends most of its time
waiting on GoHighLevel and on a model provider. A blocking database driver in
that setting is not merely slower - it is incorrect. A synchronous driver waiting
on a lock blocks the event loop, which means it blocks the very coroutine holding
that lock, and the wait can only end in a timeout. This project hit exactly that:
the concurrent-duplicate-delivery test deadlocked until the driver became async.

SQLite (via `aiosqlite`) is the default so the demo needs no services. Postgres
(via `asyncpg`) is a URL change. The tradeoff:

* SQLite has one writer at a time. Correct for a single process, and every
  guarantee in this system still holds - the UNIQUE constraint and `BEGIN
  IMMEDIATE` do the work. What it will not do is scale across workers.
* Postgres is the answer the moment there is more than one worker, or a queue
  consumer running alongside the API. `docs/architecture.md` covers the switch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from leadops.storage.schema import Base

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Drivers, keyed by the scheme a human would naturally write in DATABASE_URL.
# Accepting the sync form and upgrading it is friendlier than a startup crash
# telling someone to add "+aiosqlite" to a string they copied from the README.
_ASYNC_DRIVERS = {
    "sqlite": "sqlite+aiosqlite",
    "sqlite+pysqlite": "sqlite+aiosqlite",
    "postgresql": "postgresql+asyncpg",
    "postgresql+psycopg": "postgresql+asyncpg",
    "postgresql+psycopg2": "postgresql+asyncpg",
}


def normalise_url(database_url: str) -> str:
    scheme, separator, rest = database_url.partition("://")
    if not separator:
        return database_url
    return f"{_ASYNC_DRIVERS.get(scheme, scheme)}://{rest}"


def _configure_sqlite(engine: AsyncEngine) -> None:
    """SQLite needs explicit configuration to behave under concurrency."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # WAL lets readers proceed while a writer holds the lock.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Wait for a busy lock instead of failing instantly. Safe now that the
        # wait happens on aiosqlite's thread rather than on the event loop.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # Hand transaction control to us, so the next callback can choose the mode.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn):  # noqa: ANN001
        """Take the write lock up front.

        A DEFERRED transaction takes a read lock and tries to upgrade on its
        first write. If another writer got in between, SQLite refuses the upgrade
        *immediately* with "database is locked" - `busy_timeout` deliberately
        does not apply, because waiting on an upgrade can deadlock.

        BEGIN IMMEDIATE takes the write lock at the start, so competing writers
        queue on `busy_timeout` instead of failing. This is what makes the
        concurrent-duplicate-delivery test pass reliably rather than flakily.
        """
        conn.exec_driver_sql("BEGIN IMMEDIATE")


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _session_factory
    url = normalise_url(database_url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_async_engine(url, echo=echo, connect_args=connect_args, pool_pre_ping=True)
    if url.startswith("sqlite"):
        _configure_sqlite(_engine)
    _session_factory = async_sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised. Call init_engine() first.")
    return _engine


async def create_all() -> None:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    async with get_engine().begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transaction boundary. Commits on success, rolls back on any exception.

    Deliberately does not swallow the exception: the caller decides whether a
    failure is retryable, and it cannot decide that if the error disappears here.
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialised. Call init_engine() first.")
    session = _session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def reset_for_tests() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
