"""Retry with exponential backoff and full jitter.

Three decisions worth stating, because they are the ones that go wrong:

1. **Only retryable errors are retried.** A 400 from GHL means our request is
   malformed. Retrying it four times turns one bug into four, burns the rate
   limit, and delays the dead-letter that would have told us about it.

2. **Full jitter, not fixed backoff.** When GHL rate-limits an agency, every
   pending lead retries. Fixed backoff makes them all retry at the same instant
   and the burst repeats. `sleep = random(0, min(cap, base * 2**n))` spreads them.

3. **`Retry-After` wins.** If the server tells us when to come back, our own
   backoff calculation is not more informed than the server is.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from leadops.errors import LeadOpsError, RateLimited

T = TypeVar("T")


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 4
    base_seconds: float = 0.25
    max_seconds: float = 8.0
    # Injectable so tests are fast and deterministic rather than sleeping for real.
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    jitter: Callable[[float], float] = random.uniform

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Delay before attempt number `attempt` (1-based; attempt 1 never waits)."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_seconds * 4)
        ceiling = min(self.max_seconds, self.base_seconds * (2 ** max(0, attempt - 1)))
        return self.jitter(0.0, ceiling)


@dataclass(slots=True)
class Attempted:
    """What actually happened, so the caller can record it rather than guess."""

    value: object
    attempts: int
    duration_ms: float
    last_error: LeadOpsError | None = None


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    on_retry: Callable[[int, LeadOpsError, float], None] | None = None,
) -> Attempted:
    """Run `operation`, retrying only errors marked retryable.

    Raises the last error once attempts are exhausted; the caller decides whether
    that becomes a dead letter or a degraded-but-successful path.
    """
    started = time.perf_counter()
    last_error: LeadOpsError | None = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            value = await operation()
            return Attempted(
                value=value,
                attempts=attempt,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except LeadOpsError as exc:
            last_error = exc
            if not exc.retryable or attempt == policy.max_attempts:
                raise
            retry_after = (
                getattr(exc, "retry_after", None) if isinstance(exc, RateLimited) else None
            )
            delay = policy.delay_for(attempt, retry_after=retry_after)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await policy.sleeper(delay)

    # Unreachable: the loop either returns or raises. Kept explicit so a future
    # edit to the loop cannot silently start returning None.
    raise last_error or LeadOpsError("retry loop exhausted without result")
