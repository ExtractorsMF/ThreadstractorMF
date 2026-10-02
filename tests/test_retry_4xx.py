"""Regression: permanent 4xx responses must not be retried.

The WAF's answer is 403, and 403 never becomes a 200 by trying again. The old
loop only special-cased 429/5xx in its inline branch but then caught *every*
HTTPStatusError and backed off anyway, so a blocked request burned the full
exponential ladder (2+4+8+16+32 ~= 62s) before reporting the same error.

Sleeps are patched out: these tests assert the retry *policy*, not the waiting.
"""

import time

import httpx
import pytest

import threadstractormf.rate_limit as rl
from threadstractormf.api import ThreadsAPI

_URL = "https://www.threads.com/graphql/query"


def _api(handler, *, max_retries=5, backoff_base=2.0):
    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(
            enabled=False, max_retries=max_retries, backoff_base=backoff_base
        ),
    )
    calls = {"n": 0}

    def counted(request):
        calls["n"] += 1
        return handler(request)

    api._client = httpx.Client(transport=httpx.MockTransport(counted))
    return api, calls


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


def _status(code, headers=None):
    return lambda _r: httpx.Response(code, json={}, headers=headers or {})


@pytest.mark.parametrize("code", [400, 401, 403, 404, 405, 422])
def test_permanent_4xx_is_not_retried(code):
    api, calls = _api(_status(code))
    with pytest.raises(httpx.HTTPStatusError):
        api._request_with_rate_limit("POST", _URL, data={})
    assert calls["n"] == 1, f"status {code} must be attempted exactly once"


def test_403_fails_fast_in_wall_clock_time():
    """The regression itself: this used to take ~62s of pointless backoff."""
    api, calls = _api(_status(403))
    t0 = time.monotonic()
    with pytest.raises(httpx.HTTPStatusError):
        api._request_with_rate_limit("POST", _URL, data={})
    assert time.monotonic() - t0 < 0.5
    assert calls["n"] == 1


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_5xx_is_retried_up_to_the_limit(code):
    api, calls = _api(_status(code), max_retries=3, backoff_base=0.001)
    with pytest.raises(httpx.HTTPStatusError):
        api._request_with_rate_limit("POST", _URL, data={})
    assert calls["n"] == 4  # 1 + 3 retries


def test_429_is_retried_and_honours_retry_after():
    api, calls = _api(_status(429, {"Retry-After": "0"}), max_retries=2, backoff_base=0.001)
    with pytest.raises(httpx.HTTPStatusError):
        api._request_with_rate_limit("POST", _URL, data={})
    assert calls["n"] == 3


def test_retry_after_is_slept_when_present(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    api, _ = _api(_status(429, {"Retry-After": "7"}), max_retries=1, backoff_base=1.0)
    with pytest.raises(httpx.HTTPStatusError):
        api._request_with_rate_limit("POST", _URL, data={})
    assert 7 in slept


def test_transient_transport_errors_are_retried_then_raise():
    calls = {"n": 0}

    def handler(_r):
        calls["n"] += 1
        raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")

    api, _ = _api(handler, max_retries=2, backoff_base=0.001)
    with pytest.raises(httpx.ConnectError):
        api._request_with_rate_limit("GET", _URL)
    assert calls["n"] == 3


def test_recovers_when_a_retry_succeeds():
    calls = {"n": 0}

    def handler(_r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"ok": True}})

    api, _ = _api(handler, max_retries=3, backoff_base=0.001)
    resp = api._request_with_rate_limit("GET", _URL)
    assert resp.status_code == 200
    assert calls["n"] == 2


def test_2xx_returns_first_try():
    api, calls = _api(lambda _r: httpx.Response(200, json={"data": 1}))
    resp = api._request_with_rate_limit("GET", _URL)
    assert resp.json()["data"] == 1
    assert calls["n"] == 1
