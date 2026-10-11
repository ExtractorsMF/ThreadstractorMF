"""Shared test helpers.

Two things live here, both about keeping the suite fast and hermetic.

``no_sleep``
    Tests that exercise retry/backoff logic used to sleep for real. With
    ``RateLimitConfig(max_retries=5, backoff_base=2.0)`` a single simulated
    ``ConnectError`` cost 1+2+4+8+16 = 31 seconds of pure ``time.sleep``. Three
    test modules already monkeypatched ``time.sleep`` to avoid exactly this;
    this fixture does it once, for everything.

``no_network``
    A few CLI-level tests used to reach threads.com for real. They assert on
    where a path resolves, not on what Threads returns, so the scrape was
    paying ~27s per test to learn nothing — and worse, making the result depend
    on whether the network was up.

``seed_template``
    The GraphQL doc_id is no longer a module constant: it is captured from the
    live web app and cached. A mocked transport therefore has to supply a
    template, or the code under test reaches for Playwright — slow, and
    pointless against a MockTransport.

Exposed as a plain module-level helper: pytest puts ``tests/`` on ``sys.path``
when there is no ``__init__.py``, so ``from conftest import seed_template``
resolves. A fixture would not work from a helper function like ``_api()``.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from threadstractormf._discovery import MEDIA_QUERY_NAME, RequestTemplate


def session_cookies(sessionid: str = "test-session"):
    """A CookieJar that looks logged in.

    Discovery is gated on a session cookie, because Threads only issues the
    media query to a logged-in browser. A plain dict would not do: the
    fingerprinting walks Cookie objects, and iterating a dict yields its keys.
    """
    from threadstractormf.auth import load_cookies_dict

    return load_cookies_dict({"csrftoken": "x", "sessionid": sessionid})


def seed_template(api: Any, doc_id: str = "123456789", username: str = "user") -> Any:
    """Attach a usable request template to a mocked ThreadsAPI.

    Two details are load-bearing rather than decorative:

    * the variables carry a ``__relay_internal__pv__Barcelona*`` flag, because
      Threads refuses the query without the relay set and ``is_usable`` rejects
      a template with empty variables;
    * ``userID`` lives in the variables, since that is where the walk reads the
      target account from — the profile HTML's own ``userID`` is the *viewer's*.
    """
    api._templates[username] = RequestTemplate(
        params={
            "doc_id": doc_id,
            "lsd": "testlsd",
            "fb_api_req_friendly_name": MEDIA_QUERY_NAME,
            "fb_api_caller_class": "RelayModern",
            "server_timestamps": "true",
            "__a": "1",
            "dpr": "1",
            "__rev": "1049904309",
        },
        variables={
            "after": None,
            "first": 12,
            "userID": "999",
            "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
        },
        username=username,
        user_id="999",
    )
    api._template = api._templates[username]
    return api


@pytest.fixture(autouse=True)
def _playwright_is_assumed_available(monkeypatch):
    """Discovery tests must not depend on the browser extra.

    Every test that exercises discovery stubs ``discover_and_cache`` out, so
    nothing needs a browser — but ``_ensure_template`` calls
    ``_require_playwright()`` first, and that ImportError aborted the whole
    path. Without the extra installed, six tests failed while mocking the very
    function that would have used the browser.

    Stubbed here so the suite is honest about what it needs. The one test that
    genuinely checks the extra undoes this.
    """
    import threadstractormf.api as api_module

    monkeypatch.setattr(api_module, "_require_playwright", lambda: None)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Stop real sleeping, for every test unless one opts back in.

    Sleeping is what the retry logic is *for*, but a test can assert on the
    computed duration without paying it — ``test_retry_4xx.py`` and
    ``test_adaptive_rate_limit.py`` already did exactly this locally. Doing it
    suite-wide removes the need to remember, which is what let the slow tests
    through in the first place.

    Tests that genuinely need to wait (real rate limiting, timeout behaviour)
    opt back in with ``real_sleep``.
    """
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    yield


@pytest.fixture
def real_sleep(monkeypatch):
    """Undo :func:`no_sleep` for a test that really has to wait."""
    monkeypatch.undo()
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Keep tests off the network, and out of a real browser.

    Two entry points reach outside the process:

    * ``Threadscraper.get_posts`` / ``download_profile_pic`` — what the CLI
      calls. Unstubbed, they call threads.com for content these tests never
      look at.
    * template discovery — a rejected doc_id triggers a Playwright capture,
      which launches Chromium and loads a real profile page.

    The CLI tests assert on resolved paths and error messages, never on
    scraped content, so this keeps them to what they actually check — and makes
    them deterministic instead of dependent on whether Threads was reachable.
    A test that needs real HTTP mocks its own transport and re-patches this.
    """
    from threadstractormf.client import Threadscraper

    def unavailable(self, *args, **kwargs):
        return []

    monkeypatch.setattr(Threadscraper, "get_posts", unavailable)
    # The CLI also fetches the avatar after the posts (``--profile-pic`` is
    # implied by an unfiltered run), which is a second round trip to Threads
    # for content these tests never look at.
    monkeypatch.setattr(Threadscraper, "download_profile_pic", unavailable)

    def no_discovery(*_args, **_kwargs):
        raise RuntimeError("discovery is stubbed out in tests")

    # Discovery only runs when a template is rejected, so most tests never hit
    # this; the ones that do were spending ~12s in Chromium.
    monkeypatch.setattr("threadstractormf.api.discover_and_cache", no_discovery)
