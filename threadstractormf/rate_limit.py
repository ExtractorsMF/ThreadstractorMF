"""Anti rate-limit — port of background.js cooldown + API backoff.

background.js: cooldownMs=2000, cooldownAfter100=120000, lastCooldownMilestone
Python: FixedCooldownLimiter + BatchCooldownLimiter + ExponentialBackoff
Compatible with gallery-dl --sleep-request / yt-dlp --sleep-interval
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    cooldown_ms: int = 2000  # between downloads (background.js:16)
    cooldown_after_100_ms: int = 120000  # every 100 (background.js:17)
    jitter: float = 0.2  # 20% random to avoid thundering herd
    rps: float = 0.5  # for GraphQL API: 1 req / 2s
    max_retries: int = 5
    backoff_base: float = 2.0
    enabled: bool = True


class FixedCooldownLimiter:
    """Wait cooldown_ms between consecutive calls (CDN downloads)."""

    def __init__(self, cooldown_ms: int = 2000, jitter: float = 0.2, enabled: bool = True):
        self.cooldown_ms = cooldown_ms
        self.jitter = jitter
        self.enabled = enabled
        self._last: float | None = None

    def wait(self) -> None:
        if not self.enabled or self.cooldown_ms <= 0:
            return
        now = time.monotonic() * 1000
        if self._last is None:
            self._last = now
            return
        elapsed = now - self._last
        wait_ms = self.cooldown_ms - elapsed
        if wait_ms > 0:
            # jitter ±
            if self.jitter:
                wait_ms *= 1 + random.uniform(-self.jitter, self.jitter)
                wait_ms = max(0, wait_ms)
            time.sleep(wait_ms / 1000)
        self._last = time.monotonic() * 1000

    def reset(self) -> None:
        self._last = None


class BatchCooldownLimiter(FixedCooldownLimiter):
    """Extends Fixed with a long cooldown every N downloads (background.js batch)."""

    def __init__(
        self,
        cooldown_ms: int = 2000,
        batch_size: int = 100,
        batch_cooldown_ms: int = 120000,
        jitter: float = 0.2,
        enabled: bool = True,
    ):
        super().__init__(cooldown_ms, jitter, enabled)
        self.batch_size = batch_size
        self.batch_cooldown_ms = batch_cooldown_ms
        self._count = 0
        self._last_batch: int = 0

    def increment(self) -> None:
        self._count += 1
        # if it is a batch multiple and not the same milestone
        milestone = (self._count // self.batch_size) * self.batch_size
        if milestone > 0 and milestone != self._last_batch and self._count % self.batch_size == 0:
            self._last_batch = milestone
            if self.enabled and self.batch_cooldown_ms > 0:
                wait = self.batch_cooldown_ms / 1000
                if self.jitter:
                    wait *= 1 + random.uniform(-self.jitter, self.jitter)
                time.sleep(max(0, wait))

    def wait(self) -> None:
        super().wait()
        self.increment()


class ApiRateLimiter:
    """Simple token bucket for GraphQL: rps=0.5 => 1 req every 2s."""

    def __init__(self, rps: float = 0.5, enabled: bool = True):
        self.rps = rps
        self.enabled = enabled
        self._last: float | None = None
        self._interval = 1.0 / rps if rps > 0 else 0

    def wait(self) -> None:
        if not self.enabled or self.rps <= 0:
            return
        now = time.monotonic()
        if self._last is None:
            self._last = now
            return
        elapsed = now - self._last
        wait = self._interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()


def backoff_sleep(
    attempt: int, *, base: float = 2.0, max_sleep: float = 60.0, jitter: float = 0.3
) -> float:
    """Compute exponential sleep for 429/500. Returns seconds slept."""
    sleep = min(max_sleep, (base**attempt))
    if jitter:
        sleep *= 1 + random.uniform(-jitter, jitter)
    sleep = max(0, sleep)
    time.sleep(sleep)
    return sleep


def parse_retry_after(headers: dict) -> float | None:
    """Extract Retry-After if present (seconds)."""
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return float(str(v).split(",")[0].strip())
            except Exception:
                return None
    return None
