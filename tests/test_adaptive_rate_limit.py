"""The GraphQL limiter must back off when Meta complains, and recover after.

Pacing has to be conservative by default but responsive: a full profile still
gets walked to the end, yet a 429 must raise the spacing between requests rather
than being retried at the same speed until the WAF steps in.
"""

import json

import httpx
import pytest

import threadstractormf.rate_limit as rl
from threadstractormf.api import _MAX_PAGES, _PAGE_SIZE, ThreadsAPI
from threadstractormf.rate_limit import ApiRateLimiter

_IMAGE = {"candidates": [{"width": 1080, "url": "https://scontent.cdninstagram.com/v/t51.1/a.jpg"}]}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)


# --- limiter mechanics ------------------------------------------------------


def test_base_pacing_matches_rps():
    assert ApiRateLimiter(rps=0.5).current_interval() == pytest.approx(2.0)
    assert ApiRateLimiter(rps=1.0).current_interval() == pytest.approx(1.0)


def test_penalty_doubles_per_429():
    limiter = ApiRateLimiter(rps=0.5)
    for expected in (4.0, 8.0, 16.0, 32.0):
        limiter.penalize()
        assert limiter.current_interval() == pytest.approx(expected)


def test_penalty_is_capped():
    limiter = ApiRateLimiter(rps=0.5, max_interval=60.0)
    for _ in range(12):
        limiter.penalize()
    assert limiter.current_interval() == pytest.approx(60.0)


def test_success_relaxes_penalty_back_toward_base():
    limiter = ApiRateLimiter(rps=0.5)
    for _ in range(4):
        limiter.penalize()
    assert limiter.current_interval() > 2.0
    for _ in range(200):
        limiter.relax()
    assert limiter.current_interval() == pytest.approx(2.0)


def test_relaxation_converges_promptly():
    """An additive step needed ~1160 successes to unwind the 60s cap."""
    limiter = ApiRateLimiter(rps=0.5)
    for _ in range(6):
        limiter.penalize()
    successes = 0
    while limiter.current_interval() > 2.0 and successes < 500:
        for _ in range(limiter.relax_after):
            limiter.relax()
            successes += 1
    assert limiter.current_interval() == pytest.approx(2.0)
    assert successes <= 120, f"recovery took {successes} successes"


def test_relax_does_nothing_without_a_penalty():
    limiter = ApiRateLimiter(rps=0.5)
    for _ in range(50):
        limiter.relax()
    assert limiter.penalty == pytest.approx(1.0)


def test_disabled_limiter_never_penalizes():
    limiter = ApiRateLimiter(rps=0.5, enabled=False)
    limiter.penalize()
    limiter.relax()
    assert limiter.current_interval() == pytest.approx(2.0)


def test_zero_rps_does_not_divide_by_zero():
    """penalize() used to compute max_interval / base_interval unguarded."""
    limiter = ApiRateLimiter(rps=0)
    assert limiter.current_interval() == 0.0
    limiter.penalize()
    assert limiter.current_interval() == 0.0


# --- wiring into the request path -------------------------------------------


def _page(codes, *, has_next, cursor):
    return {
        "data": {
            "mediaData": {
                "edges": [
                    {
                        "node": {
                            "thread_items": [
                                {
                                    "post": {
                                        "pk": str(1000 + i),
                                        "code": c,
                                        "user": {"username": "user"},
                                        "taken_at": 1700000000,
                                        "image_versions2": _IMAGE,
                                    }
                                }
                            ]
                        }
                    }
                    for i, c in enumerate(codes)
                ],
                "page_info": {"has_next_page": has_next, "end_cursor": cursor},
            }
        }
    }


def _walk(pages, limit=None):
    calls = {"n": 0}

    def handler(_r):
        i = calls["n"]
        calls["n"] += 1
        return httpx.Response(
            200, content=json.dumps(pages[min(i, len(pages) - 1)]).encode()
        )

    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(rps=0.5, enabled=False),
    )
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    return api.get_posts("user", limit=limit), calls, api


def test_429_raises_the_pacing_for_the_rest_of_the_walk():
    calls = {"n": 0}

    def handler(_r):
        i = calls["n"]
        calls["n"] += 1
        if i == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        body = _page(
            [f"P{i}_{j}" for j in range(_PAGE_SIZE)], has_next=i < 6, cursor=f"c{i}"
        )
        return httpx.Response(200, content=json.dumps(body).encode())

    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(rps=0.5, max_retries=1, backoff_base=0.001),
    )
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    posts = api.get_posts("user", limit=None)

    assert posts, "the crawl must still complete"
    assert api._api_limiter.penalty > 1.0
    assert api._api_limiter.current_interval() > api._api_limiter.base_interval
    # the configured base rate itself is never mutated
    assert api._api_limiter.base_interval == pytest.approx(2.0)


def test_clean_walk_never_penalizes():
    pages = [
        _page([f"Q{i}_{j}" for j in range(_PAGE_SIZE)], has_next=i < 3, cursor=f"c{i}")
        for i in range(4)
    ]
    _posts, _calls, api = _walk(pages)
    assert api._api_limiter.penalty == pytest.approx(1.0)


# --- page budget ------------------------------------------------------------


def test_max_pages_covers_a_whole_large_profile():
    assert _MAX_PAGES * _PAGE_SIZE >= 2000


def test_limit_derives_a_tighter_page_budget():
    """A small --limit must not trigger a long walk."""
    pages = [
        _page([f"W{i}_{j}" for j in range(_PAGE_SIZE)], has_next=True, cursor=f"c{i}")
        for i in range(6)
    ]
    _posts, calls, _api = _walk(pages, limit=15)
    assert calls["n"] <= 4  # ceil(15/12) + 2


def test_no_limit_uses_the_full_budget():
    pages = [
        _page([f"V{i}_{j}" for j in range(_PAGE_SIZE)], has_next=i < 4, cursor=f"c{i}")
        for i in range(5)
    ]
    _posts, calls, _api = _walk(pages, limit=None)
    assert calls["n"] == 5


# --- partial results survive a mid-walk failure -----------------------------


def test_posts_collected_before_a_failure_are_kept():
    """Regression: losing 40 pages of results because page 41 timed out."""

    calls = {"n": 0}

    def handler(_r):
        i = calls["n"]
        calls["n"] += 1
        if i < 2:
            body = _page(
                [f"K{i}_{j}" for j in range(_PAGE_SIZE)], has_next=True, cursor=f"c{i}"
            )
            return httpx.Response(200, content=json.dumps(body).encode())
        raise httpx.ConnectError("page 3 dropped")

    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(enabled=False, max_retries=0),
    )
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    api._fetch_posts_via_playwright = lambda _u, _l: ["FALLBACK"]

    posts = api.get_posts("user", limit=None)
    assert len(posts) == 2 * _PAGE_SIZE
    assert posts != ["FALLBACK"]


def test_falls_back_to_the_dom_scraper_when_nothing_was_collected():
    def handler(_r):
        raise httpx.ConnectError("first page dropped")

    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(enabled=False, max_retries=0),
    )
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    api._fetch_posts_via_playwright = lambda _u, _l: ["FALLBACK"]

    assert api.get_posts("user", limit=None) == ["FALLBACK"]


def test_partial_results_respect_the_limit():
    calls = {"n": 0}

    def handler(_r):
        i = calls["n"]
        calls["n"] += 1
        if i == 0:
            body = _page(
                [f"M{j}" for j in range(_PAGE_SIZE)], has_next=True, cursor="c0"
            )
            return httpx.Response(200, content=json.dumps(body).encode())
        raise httpx.ConnectError("second page dropped")

    api = ThreadsAPI(
        {"csrftoken": "x"},
        rate_limit_config=rl.RateLimitConfig(enabled=False, max_retries=0),
    )
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    api._fetch_posts_via_playwright = lambda _u, _l: ["FALLBACK"]

    assert len(api.get_posts("user", limit=5)) == 5
