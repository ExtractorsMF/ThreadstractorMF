import time

from threadstractormf.rate_limit import (
    ApiRateLimiter,
    BatchCooldownLimiter,
    backoff_sleep,
    parse_retry_after,
)


def test_batch_cooldown_disabled_fast():
    limiter = BatchCooldownLimiter(cooldown_ms=0, batch_size=2, batch_cooldown_ms=0, enabled=False)
    start = time.monotonic()
    for _ in range(3):
        limiter.wait()
    assert time.monotonic() - start < 0.2


def test_fixed_cooldown_wait():
    limiter = BatchCooldownLimiter(
        cooldown_ms=100, batch_size=100, batch_cooldown_ms=0, jitter=0, enabled=True
    )
    limiter.wait()  # first no wait
    t0 = time.monotonic()
    limiter.wait()
    assert time.monotonic() - t0 >= 0.08  # ~100ms


def test_api_limiter():
    limiter = ApiRateLimiter(rps=10, enabled=True)  # 0.1s interval
    limiter.wait()
    t0 = time.monotonic()
    limiter.wait()
    assert time.monotonic() - t0 >= 0.08


def test_parse_retry_after():
    assert parse_retry_after({"Retry-After": "120"}) == 120
    assert parse_retry_after({"retry-after": "2.5"}) == 2.5
    assert parse_retry_after({}) is None


def test_backoff_quick():
    t0 = time.monotonic()
    backoff_sleep(0, base=0.01, max_sleep=0.05, jitter=0)
    assert time.monotonic() - t0 < 0.2
