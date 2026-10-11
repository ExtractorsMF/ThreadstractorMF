"""The suite-wide test fixtures: they must actually be doing their job.

Both fixtures here are ``autouse=True``, which is the only thing keeping the
suite fast and offline. Dropping ``autouse=True`` is an invisible edit: the
fixture still works, every test that asks for it by name still passes, and the
suite silently goes back to sleeping through real backoffs and launching
Chromium. It happened once already — inserting a new autouse fixture next to
``no_sleep`` cost it its ``autouse=True``, and the suite went from 3.5s to 207s
with every test still green.

These tests fail loudly if either fixture stops applying itself.
"""

from __future__ import annotations

import http.cookiejar
import time

import httpx

import threadstractormf.rate_limit as rl
from threadstractormf.api import ThreadsAPI
from threadstractormf.auth import load_cookies_dict


def test_sleeping_is_stubbed_out_unless_a_test_asks_otherwise():
    """``no_sleep`` must stay autouse.

    Asserted through the real backoff path rather than by patching
    ``time.sleep``: a template is seeded so the POST is actually attempted, its
    transport raises ``ConnectError``, and ``_request_with_rate_limit`` walks
    its five retries. Without the fixture that costs 1+2+4+8+16 = 31 seconds.

    An earlier version of this test skipped the template and passed even with
    the fixture removed — the walk aborted before any request, so there was no
    backoff to measure.
    """
    import threadstractormf._discovery as disc

    api = ThreadsAPI(
        load_cookies_dict({"csrftoken": "x", "sessionid": "s"}),
        rate_limit_config=rl.RateLimitConfig(max_retries=5, backoff_base=2.0),
    )
    api._resolve_user_id = lambda _u: "1"
    api._templates["u"] = disc.RequestTemplate(
        params={"doc_id": "D", "lsd": "L"},
        variables={"after": None, "userID": "1"},
        username="u",
    )
    api._fetch_posts_via_playwright = lambda _u, _l: []

    def always_unavailable(_request):
        raise httpx.ConnectError("down")

    api._client = httpx.Client(transport=httpx.MockTransport(always_unavailable))

    before = time.monotonic()
    api.get_posts("u", limit=5)
    elapsed = time.monotonic() - before

    assert elapsed < 2.0, f"backoff waited {elapsed:.1f}s — no_sleep is not active"


def test_discovery_does_not_need_the_browser_extra():
    """``_require_playwright`` is stubbed, so no test needs the extra.

    A test that mocks ``discover_and_cache`` must not fail because Playwright is
    unimportable: it mocked the very function that would have used the browser.
    """
    import threadstractormf.api as api_module

    # The autouse fixture has already patched it; calling it must be harmless.
    api_module._require_playwright()


def test_an_anonymous_cookies_jar_looks_logged_out():
    """``session_cookies`` and the raw-jar cases must stay distinguishable."""
    from threadstractormf._discovery import session_fingerprint

    assert session_fingerprint(http.cookiejar.CookieJar()) == "anonymous"
    assert session_fingerprint(load_cookies_dict({"csrftoken": "x"})) == "anonymous"
    assert session_fingerprint(
        load_cookies_dict({"csrftoken": "x", "sessionid": "s"})
    ) != "anonymous"
