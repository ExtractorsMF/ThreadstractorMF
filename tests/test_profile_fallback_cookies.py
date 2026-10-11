"""The DOM fallback must read the profile anonymously.

Measured on a 9-post profile: with the session cookies attached the fallback
saw 4 posts, without them all 9. Threads serves a logged-in client a truncated
profile view whose own tab query answers has_next_page=false after four posts,
so injecting the session silently lost five of nine — with no warning, since
nothing about the response looks like an error.

This is the regression guard for that. It pins the behaviour that matters:
cookies in, but the profile still comes back whole. It is deliberately not a
test of Chromium's cookie semantics (tests/test_backend.py covers that) but of
the decision this module makes about them.
"""

import http.cookiejar

import pytest

from threadstractormf.api import (
    _CEILING_REASON,
    _LIMIT_REASON,
    _PROFILE_FALLBACK_AUTHENTICATED,
    _STOP_RENDERING_REASON,
    ThreadsAPI,
)


def _jar() -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for name, domain in (
        ("sessionid", ".threads.com"),
        ("csrftoken", ".threads.com"),
        ("ds_user_id", ".threads.com"),
    ):
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=f"v-{name}",
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


class _FakePage:
    """Records what the fallback injected and how hard it scrolled."""

    def __init__(self, posts: int, growing: bool = False) -> None:
        self.posts = posts
        # A page that keeps rendering never trips the empty-rounds break, so the
        # walk runs its full course and hits the scroll ceiling instead.
        self.growing = growing
        self.added: list[list[dict[str, str]]] = []
        self.wheel_calls = 0

    def on(self, *_args):
        return None

    def goto(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, _ms):
        return None

    @property
    def mouse(self):
        # A property, like the real Playwright API: the fallback calls
        # page.mouse.wheel(...), so mouse must not be a bound method.
        return self

    def wheel(self, *_args):
        self.wheel_calls += 1

    def evaluate(self, script: str):
        # The counter script is the one that evaluates to a bare number:
        # "() => document.querySelectorAll('time[datetime]').length".
        if script.rstrip().endswith(".length"):
            # The walk counts rendered <time> elements to decide whether the
            # profile grew; a stable count models a page that is done loading.
            if self.growing:
                self.posts += 1
            return self.posts
        # The big extractor script returns a list of post dicts.
        return [
            {
                "permalink": f"https://www.threads.com/@u/post/P{i}",
                "datetime": f"2025-01-0{i + 1}T00:00:00.000Z",
                "media_urls": [f"https://scontent.cdninstagram.com/v/{i}.jpg"],
            }
            for i in range(self.posts)
        ]

    def close(self):
        return None


class _FakeCtx:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    def add_cookies(self, cookies):
        self._page.added.append(cookies)

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    def new_context(self, **_kwargs):
        return _FakeCtx(self._page)

    def close(self):
        return None


class _FakeChromium:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    def launch(self, **_kwargs):
        return _FakeBrowser(self._page)


def _install(page, monkeypatch):
    """Wire a fake Playwright that hands the fallback our recording page."""
    import sys
    import types

    module = types.ModuleType("playwright.sync_api")

    class _CtxManager:
        def __enter__(self):
            return types.SimpleNamespace(chromium=_FakeChromium(page))

        def __exit__(self, *_exc):
            return False

    def sync_playwright():
        return _CtxManager()

    module.sync_playwright = sync_playwright  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return module


# --- the decision itself ----------------------------------------------------


def test_the_profile_fallback_is_anonymous_by_default():
    """The whole fix in one assertion.

    Defaulting to authenticated is what lost five of nine posts in v1.1.0.
    """
    assert _PROFILE_FALLBACK_AUTHENTICATED is False


def test_an_authenticated_fallback_does_inject_cookies(monkeypatch):
    """Opting in still works, so the flag is a choice and not a removal."""
    page = _FakePage(9)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())
    api._fetch_posts_via_playwright("u", 10, authenticated=True)
    assert page.added, "an authenticated run must still attach the session"
    names = {c["name"] for c in page.added[0]}
    assert "sessionid" in names


def test_the_anonymous_default_injects_nothing(monkeypatch):
    page = _FakePage(9)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())
    api._fetch_posts_via_playwright("u", 10)
    # add_cookies is skipped entirely when the list is empty, which is the
    # point: the anonymous context must receive nothing at all.
    assert page.added == [], "no session may reach the profile page view"


def test_the_cookie_dot_is_still_preserved_where_it_matters():
    """The dot must stay for the paths that need it.

    Reverting the dot everywhere would restore the full profile and break
    threads.net session cookies at the same time. That is why this is a
    parameter: the domain-level cookie stays correct for downloads and GraphQL.
    """
    from threadstractormf._backend import to_playwright_cookies

    jar = _jar()
    out = to_playwright_cookies(jar)
    assert {c["domain"] for c in out} == {".threads.com"}
    assert all(c["domain"].startswith(".") for c in out)


# --- pacing, so the walk does not regress either ----------------------------


def test_the_scroll_walk_is_patient_enough(monkeypatch):
    """v1.0.1 needed 3000ms per scroll; 91a3a45 cut it to ~1.8s.

    That change was blamed for the truncation and turned out not to be the
    cause, but the longer wait is strictly safer for a lazy-loading timeline,
    so it is pinned here to stop it drifting down again.
    """
    from threadstractormf.api import _SCROLL_JITTER_SCROLLS, _SCROLL_WAIT_MS

    assert _SCROLL_WAIT_MS >= 2500, "a shorter wait risks stopping early"
    assert _SCROLL_JITTER_SCROLLS >= 1


def test_a_slow_first_batch_is_still_scrolled_through(monkeypatch):
    """The walk must continue past a batch that rendered nothing yet."""
    page = _FakePage(9)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())
    api._fetch_posts_via_playwright("u", 100)
    assert page.wheel_calls > 5, "it gave up before working through the profile"


@pytest.mark.parametrize("authenticated", [True, False])
def test_the_flag_does_not_change_what_is_returned(monkeypatch, authenticated):
    """Both branches read the same posts; only the cookies differ.

    Renamed from ``..._return_type``: the return is now a DomScrape rather than
    a bare list, so that name states something false. The posts inside are
    unchanged, which is what this test is actually about — the
    ``authenticated`` flag must not affect coverage, only which account's view
    of it is read.
    """
    page = _FakePage(4)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())
    out = api._fetch_posts_via_playwright("u", 10, authenticated=authenticated)
    assert [p.id for p in out.posts] == [f"P{i}" for i in range(4)]


# --- why the walk stopped ---------------------------------------------------
#
# The three exits below used to be indistinguishable: the caller got the same
# list either way, and the report kept whatever reason the failed API attempt
# had left behind. So a browser scrape that ran out of scrolls was summarised
# with "the API rejected the query", naming a walk that had already handed over.


def test_a_page_that_keeps_rendering_reports_the_scroll_ceiling(monkeypatch):
    """Never breaking out is the one exit that *proves* truncation."""
    page = _FakePage(2, growing=True)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())

    out = api._fetch_posts_via_playwright("u", None)

    assert out.reason == _CEILING_REASON
    assert out.exhausted is False, "the ceiling is not completion"


def test_a_page_that_stops_rendering_is_reported_as_such(monkeypatch):
    """The end of the timeline, as far as the walk can tell.

    ``exhausted`` stays False here on purpose: "stopped rendering" is a
    heuristic, and it is the one that once returned 4 posts out of 9 in silence.
    The reason is recorded so the summary can say it, without claiming the
    profile was fully read.
    """
    page = _FakePage(4)  # a stable count: nothing new renders
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())

    out = api._fetch_posts_via_playwright("u", None)

    assert out.reason == _STOP_RENDERING_REASON
    assert out.exhausted is False, "not proof of a complete profile"
    assert len(out.posts) == 4


def test_hitting_the_limit_is_reported_as_the_limit(monkeypatch):
    page = _FakePage(4)
    _install(page, monkeypatch)
    api = ThreadsAPI(_jar())

    out = api._fetch_posts_via_playwright("u", 2)

    assert out.reason == _LIMIT_REASON
    assert len(out.posts) == 2, "the limit is applied to the posts"
    # Asking for less is not truncation, whatever the heuristic says.
    assert out.exhausted is True
