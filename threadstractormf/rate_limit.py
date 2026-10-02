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
    """Paced limiter for GraphQL requests, with adaptive back-pressure.

    Base pacing is a plain interval (``rps=0.5`` -> one request every 2s), which
    is deliberately slow because Meta answers a burst with 429s and, eventually,
    with a WAF block.

    On top of that the limiter reacts to what the server actually says:

    * a 429 **doubles** the interval (capped at ``max_interval``), so repeated
      complaints space requests out exponentially instead of hammering;
    * a streak of successful requests **relaxes** it back toward the base rate,
      one step at a time, so a single 429 does not permanently slow a crawl.

    Net effect: a full profile still gets walked to the end, but the request rate
    backs off exactly as much as the server demands and recovers on its own.
    """

    def __init__(
        self,
        rps: float = 0.5,
        enabled: bool = True,
        *,
        max_interval: float = 60.0,
        penalty_factor: float = 2.0,
        relax_after: int = 5,
        relax_factor: float = 0.75,
    ):
        self.rps = rps
        self.enabled = enabled
        self.max_interval = max_interval
        self.penalty_factor = penalty_factor
        self.relax_after = relax_after
        self.relax_factor = relax_factor
        self._last: float | None = None
        self._base_interval = 1.0 / rps if rps > 0 else 0.0
        self._penalty = 1.0
        self._ok_streak = 0

    @property
    def base_interval(self) -> float:
        return self._base_interval

    @property
    def penalty(self) -> float:
        return self._penalty

    def current_interval(self) -> float:
        """Effective seconds to wait between requests right now."""
        if self._base_interval <= 0:
            return 0.0
        return min(self._base_interval * self._penalty, self.max_interval)

    def _max_penalty(self) -> float:
        if self._base_interval <= 0:
            return 1.0
        return self.max_interval / self._base_interval

    def penalize(self) -> None:
        """Call on 429 (or any rate-limit signal): back off harder."""
        if not self.enabled:
            return
        self._penalty = min(self._penalty * self.penalty_factor, self._max_penalty())
        self._ok_streak = 0

    def relax(self) -> None:
        """Call on success: decay the penalty back toward the base rate.

        Decay is multiplicative so recovery from a deep penalty converges in a
        handful of streaks. An additive step needed ~1160 successful requests to
        climb back from the 60s cap, which in practice never happened.
        """
        if not self.enabled or self._penalty <= 1.0:
            return
        self._ok_streak += 1
        if self._ok_streak >= self.relax_after:
            self._penalty = max(1.0, self._penalty * self.relax_factor)
            self._ok_streak = 0

    def wait(self) -> None:
        if not self.enabled or self._base_interval <= 0:
            return
        now = time.monotonic()
        if self._last is None:
            self._last = now
            return
        elapsed = now - self._last
        wait = self.current_interval() - elapsed
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
