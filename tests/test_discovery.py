"""The doc_id must be discovered, not hardcoded.

The bug this file exists for: v1.2.0 shipped ``doc_id 37598244946487292`` as a
module constant. Meta rotates that id whenever the persisted query changes, and
when it does the endpoint answers

    {"errors": [{"message": "execution error", "severity": "CRITICAL"}]}

with no data — indistinguishable from an empty profile. The fast path was then
skipped entirely and every scrape fell through to the slow, truncating DOM
fallback, with no constant anyone could see the age of.

Two things are covered here:

* discovery produces a *replayable* template from a captured request, keeping
  only the fields measured stable across pages;
* a rejected template recovers by itself — rediscover, retry once — so the
  next rotation needs no release.
"""

import json
import os
import sys
import types
from urllib.parse import urlencode

import httpx
import pytest
from conftest import session_cookies

import threadstractormf._discovery as disc
import threadstractormf.rate_limit as rl
from threadstractormf._discovery import (
    MEDIA_QUERY_NAME,
    STABLE_FIELDS,
    RequestTemplate,
    build_body,
    clear_template,
    discover_and_cache,
    load_template,
    save_template,
)
from threadstractormf.api import ThreadsAPI


class _FakeRequest:
    def __init__(self, url, body):
        self.url = url
        self.post_data = body

    def __str__(self):  # pragma: no cover - debugging aid
        return f"<FakeRequest {self.url}>"


class _FakePage:
    """Just enough page for discover(): emit requests, then scroll."""

    def __init__(self, requests):
        self._requests = requests
        self._handlers = []

    def on(self, event, handler):
        assert event == "request"
        self._handlers.append(handler)

    def goto(self, _url, **_kwargs):
        # The web app fires its GraphQL queries once the page loads; that is
        # the moment discover() is listening for.
        for req_url, body in self._requests:
            for handler in self._handlers:
                handler(_FakeRequest(req_url, urlencode(body)))

    def wait_for_timeout(self, _ms):
        pass

    def mouse(self):
        return self

    def wheel(self, *_args):
        pass


def _fake_browser(monkeypatch, requests):
    """Install a Playwright double that replays ``requests``.

    Lets :func:`discover` be tested for real — its filtering, its user-id
    extraction, its error path — without launching Chromium or touching the
    network.
    """
    page = _FakePage(requests)

    class _Browser:
        def new_context(self, **_kwargs):
            return types.SimpleNamespace(
                add_cookies=lambda _c: None, new_page=lambda: page
            )

        def close(self):
            pass

    class _Ctx:
        def __enter__(self):
            return types.SimpleNamespace(
                chromium=types.SimpleNamespace(launch=lambda **_k: _Browser())
            )

        def __exit__(self, *_exc):
            return False

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _Ctx()
    package = types.ModuleType("playwright")
    package.sync_api = module
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)


def _captured_vars(**overrides):
    """The captured ``variables`` blob, as the web app sends it.

    Carries the ``__relay_internal__pv__Barcelona*`` feature flags, which are
    mandatory: Threads answers ``execution error`` when they are missing.
    """
    v = {"after": None, "allow_page_info_for_lox_user": False, "before": None,
         "first": 10, "last": None, "userID": "17841433958157014",
         "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": True}
    v.update(overrides)
    return v


def _captured_body(**overrides):
    """A request body shaped like the one the web app sends."""
    body = {
        "av": "17841433958157014",
        "__user": "0",
        "__a": "1",
        "__req": "u",
        "dpr": "1",
        "__ccg": "EXCELLENT",
        "__rev": "1049904309",
        "__s": "su7szb:1rxe4g",
        "__hsi": "7694898379082016319",
        "__dyn": "7xe6E5q5U5ObwKBAg5S1Dxu1",
        "__csr": "gAIAYl2A4dMzb2lsIRkJP-ytJnnsRlFYWEx59gilOJ",
        "__hsdp": "jE5Fc0ENj84k3Iee2p2DEw-6kR0wg",
        "__comet_req": "29",
        "fb_dtsg": "NAfxutUU9ls0XgD9nXIp9izHm8qpu7UtOV8ki76DTOG2114VfEfpjvA",
        "jazoest": "26273",
        "lsd": "30uELDY4-JXYw3NmDasBWc",
        "__spin_r": "1049904309",
        "__spin_b": "trunk",
        "__crn": "comet.threads.BarcelonaProfileMediaColumnRoute",
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": MEDIA_QUERY_NAME,
        "server_timestamps": "true",
        "variables": json.dumps(
            {
                "after": None,
                "first": 10,
                "userID": "17841433958157014",
                "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": True,
            }
        ),
        "doc_id": "29965387339717034",
    }
    body.update(overrides)
    return body


# --- discovery ------------------------------------------------------------


def test_discovery_keeps_the_stable_fields_and_drops_the_nonces(monkeypatch):
    """Only what was measured identical across pages is replayed.

    The per-request nonces (``__csr``, ``__dyn``...) changed between page 1 and
    page 3 of the same scroll, so replaying a captured one is not meaningful.
    They are kept separately for the day they become mandatory.

    This drives the real :func:`discover` through a fake browser rather than
    re-implementing its filtering inline: a test that rebuilds the logic it is
    meant to check passes even when the code under it is wrong.
    """
    body = _captured_body()
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", body)])

    got = disc.discover("user", [])

    assert got.doc_id == "29965387339717034"
    assert got.params["lsd"] == body["lsd"]
    assert got.params["fb_dtsg"] == body["fb_dtsg"]
    # The nonces are captured but not replayed.
    assert "__csr" in got.nonces
    assert "__dyn" in got.nonces
    assert "__csr" not in got.params
    assert "__dyn" not in got.params
    # Everything replayed must be a stable field.
    assert set(got.params) <= set(STABLE_FIELDS)
    # And the lsd must be present, or the request cannot be signed.
    assert got.is_usable()


def test_discovery_reads_the_user_id_out_of_the_captured_variables(monkeypatch):
    """The template records whose account the tokens belong to.

    Not used for the request itself — ``variables.userID`` is supplied per
    scrape — but it is what makes a captured template identifiable when
    debugging a rejection.
    """
    body = _captured_body(
        variables=json.dumps({"after": None, "userID": "17841433958157014"})
    )
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", body)])

    assert disc.discover("user", []).user_id == "17841433958157014"


def test_discovery_survives_variables_it_cannot_parse(monkeypatch):
    """A malformed variables blob must not yield a usable template.

    The relay flags live in ``variables`` and Threads refuses the query without
    them, so a template built from an undecodable blob would look like a capture
    and then fail on every replay. Raising is the honest outcome.
    """
    body = _captured_body(variables="not json at all")
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", body)])

    with pytest.raises(RuntimeError):
        disc.discover("user", [])

def test_discovery_raises_when_no_matching_request_is_seen(monkeypatch):
    """The signal that Threads changed its client, not a silent empty template.

    Returning a half-built template here would replay a request that cannot
    work; raising lets the caller fall back and say so.
    """
    _fake_browser(monkeypatch, [])
    with pytest.raises(RuntimeError, match="BarcelonaProfileMediaTabRefetchableDirectQuery"):
        disc.discover("user", [])


def test_discovery_ignores_other_graphql_traffic(monkeypatch):
    """Only the profile-media query yields a replayable template.

    Threads fires several GraphQL queries on load; the experiment query shares
    the endpoint but carries a different doc_id, so capturing it would poison
    the cache with an id for the wrong query.
    """
    other = _captured_body(
        fb_api_req_friendly_name="fetchBarcelonaExperimentQuery",
        doc_id="26349442214668312",
    )
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", other)])

    with pytest.raises(RuntimeError):
        disc.discover("user", [])


def test_discovery_rejects_a_request_without_an_lsd(monkeypatch):
    """An lsd-less capture is unusable and must not be cached as if it worked."""
    body = _captured_body(lsd="")
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", body)])

    with pytest.raises(RuntimeError):
        disc.discover("user", [])


def test_a_second_scrape_reuses_the_cached_template_without_a_browser(
    monkeypatch, tmp_path
):
    """The whole point of caching: pay for the browser once per session."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    body = _captured_body()
    _fake_browser(monkeypatch, [("https://www.threads.com/graphql/query", body)])

    first = disc.discover_and_cache("user", [])
    assert first.doc_id == "29965387339717034"

    # Now make any further browser use an error: a second discover must not run.
    def explode(*_a, **_kw):
        raise AssertionError("discovery must not run when the cache is warm")

    monkeypatch.setattr(disc, "discover", explode)
    cached = disc.load_template(disc.session_fingerprint([]), "user")
    assert cached is not None
    assert cached.doc_id == "29965387339717034"


def _template(doc_id="1", lsd="x", **kw):
    """A minimal *usable* template: id, lsd and the relay flags."""
    return RequestTemplate(
        params={"doc_id": doc_id, "lsd": lsd},
        variables={
            "after": None,
            "userID": "999",
            "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
        },
        **kw,
    )


def test_build_body_swaps_variables_and_keeps_everything_else():
    """Pagination must be expressible without touching the captured params."""
    template = RequestTemplate(
        params={k: _captured_body().get(k, "v") for k in STABLE_FIELDS},
        variables=_captured_vars(),
        user_id="1",
    )
    page1 = build_body(template, {"after": None, "first": 12, "userID": "1"})
    page2 = build_body(template, {"after": "CURSOR1", "first": 12, "userID": "1"})

    assert page1["doc_id"] == page2["doc_id"]
    assert page1["lsd"] == page2["lsd"]
    assert page1["fb_dtsg"] == page2["fb_dtsg"]
    assert json.loads(page1["variables"])["after"] is None
    assert json.loads(page2["variables"])["after"] == "CURSOR1"


def test_build_body_merges_rather_than_replaces_the_variables():
    """The regression that made every replay fail with ``execution error``.

    Measured against the live endpoint: the captured body answered with 12
    edges, and the same body with only our seven keys answered 0 edges and one
    ``execution error``. The cause is the ~38
    ``__relay_internal__pv__Barcelona*relayprovider`` feature flags the query
    is validated against — they live in ``variables`` and must be carried over
    verbatim, with only our own fields layered on top.
    """
    template = _template(doc_id="D", lsd="L")
    template.variables = _captured_vars(
        __relay_internal__pv__BarcelonaHasCommunityTopContributorsrelayprovider=False,
        __relay_internal__pv__BarcelonaIsCrawlerrelayprovider=False,
    )

    body = build_body(template, {"userID": "42", "after": "CUR", "first": 12})
    sent = json.loads(body["variables"])

    # Our fields won.
    assert sent["userID"] == "42"
    assert sent["after"] == "CUR"
    assert sent["first"] == 12
    # The captured flags survived.
    assert sent["__relay_internal__pv__BarcelonaHasCommunityTopContributorsrelayprovider"] is False
    assert sent["__relay_internal__pv__BarcelonaIsCrawlerrelayprovider"] is False
    assert sent["__relay_internal__pv__BarcelonaIsLoggedInrelayprovider"] is True
    # And the template itself was not mutated by building a body from it.
    assert template.variables["userID"] == "17841433958157014"


def test_a_template_without_an_id_lsd_or_variables_is_not_usable():
    """Half a template must be rejected, not replayed.

    Sending a request without an lsd, or without the relay flags, is exactly
    the failure this module exists to stop, so it has to fail at load time
    instead of turning into a rejected request on every page.
    """
    ok = RequestTemplate(
        params={"doc_id": "1", "lsd": "x"},
        variables={"userID": "1", "__relay_internal__pv__X": True},
    )
    assert ok.is_usable()

    assert not RequestTemplate(params={"lsd": "x"}, variables={"a": 1}).is_usable()
    assert not RequestTemplate(params={"doc_id": "1"}, variables={"a": 1}).is_usable()
    # variables are mandatory, not optional
    assert not RequestTemplate(params={"doc_id": "1", "lsd": "x"}).is_usable()

# --- persistence ----------------------------------------------------------


def test_a_template_survives_a_round_trip_through_disk(tmp_path, monkeypatch):
    """The whole point of the cache is paying for the browser once."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    template = _template(doc_id="999", lsd="tok", nonces={"__csr": "c"},
                         user_id="42", captured_at=123.0, username="user")
    save_template(template)

    loaded = load_template(username="user")
    assert loaded is not None
    assert loaded.doc_id == "999"
    assert loaded.user_id == "42"
    assert loaded.nonces["__csr"] == "c"
    # The relay flags must survive the round trip: a template that lost them
    # would load fine and then be refused on every single request.
    assert loaded.variables["__relay_internal__pv__BarcelonaIsLoggedInrelayprovider"] is False
    assert loaded.is_usable()

    clear_template(username="user")
    assert load_template(username="user") is None


def test_a_corrupt_cache_is_treated_as_absent_not_fatal(tmp_path, monkeypatch):
    """Discovery's own failure path must not be a crash."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    path = disc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_template() is None


def test_an_unwritable_cache_does_not_break_a_working_scrape(tmp_path, monkeypatch):
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "ro"))
    (tmp_path / "ro").write_text("a file, not a directory", encoding="utf-8")
    # Must not raise: the template still works in memory for this run.
    save_template(_template())


def test_stale_templates_are_pruned_when_a_new_one_is_saved(tmp_path, monkeypatch):
    """One file per (session, account), so runs accumulate forever.

    Every logout/login and every new profile adds a file whose tokens are
    already dead, and nothing removed them. The freshly written template is
    never swept along.
    """
    import time as _time

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    fp = disc.session_fingerprint(session_cookies())
    save_template(_template(doc_id="OLD", lsd="old", username="old"), fp, "old")

    cache = disc._cache_path(fp, "old").parent
    # Backdate every existing template: ours is written after the sweep.
    stale = _time.time() - 100 * 86400
    for f in cache.iterdir():
        os.utime(f, (stale, stale))

    save_template(_template(doc_id="NEW", lsd="new", username="new"), fp, "new")

    remaining = {p.name for p in cache.iterdir()}
    assert disc._cache_path(fp, "new").name in remaining, "must survive"
    assert disc._cache_path(fp, "old").name not in remaining, "stale one goes"


def test_a_recently_used_template_is_not_pruned(tmp_path, monkeypatch):
    """The sweep is conservative: only clearly-unused files are removed."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    save_template(_template(username="keep"))
    save_template(_template(username="other"))

    cache = disc._cache_path("fp", "keep").parent
    assert len(list(cache.iterdir())) == 2, "neither is old enough to prune"


def test_pruning_ignores_directories_and_unrelated_files(tmp_path, monkeypatch):
    """Only plain files this module wrote are ever candidates."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    cache = disc._cache_path("fp", "u").parent
    cache.mkdir(parents=True, exist_ok=True)
    subdir = cache / "not-a-template"
    subdir.mkdir()
    other = cache / "notes.txt"
    other.write_text("leave me", encoding="utf-8")

    save_template(_template(username="u"))

    assert subdir.is_dir()
    assert other.exists()


# --- automatic recovery ---------------------------------------------------


def _page(codes, *, has_next=False, cursor=None, username="user"):
    image = {"candidates": [{"width": 1080, "url": "https://scontent.cdninstagram.com/v/a.jpg"}]}
    edges = [
        {
            "node": {
                "thread_items": [
                    {
                        "post": {
                            "pk": str(1000 + i),
                            "code": code,
                            "user": {"username": username},
                            "caption": {"text": "t"},
                            "taken_at": 1700000000,
                            "image_versions2": image,
                        }
                    }
                ]
            }
        }
        for i, code in enumerate(codes)
    ]
    return {
        "data": {
            "mediaData": {
                "edges": edges,
                "page_info": {"has_next_page": has_next, "end_cursor": cursor},
            }
        }
    }


def _stale():
    return {
        "errors": [{"message": "execution error", "severity": "CRITICAL"}],
        "data": {},
        "status": "fail",
    }


def _api(responses, monkeypatch, tmp_path):
    """A mocked API whose discovery is stubbed, so no browser is launched."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    calls = {"n": 0}

    def handler(_request):
        i = calls["n"]
        calls["n"] += 1
        payload = responses[min(i, len(responses) - 1)]
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    # A session cookie is required: browser-based discovery is skipped without
    # one, because Threads only issues the media query to a logged-in browser.
    api = ThreadsAPI(session_cookies(), rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    api._test_calls = calls
    return api


def test_a_second_profile_can_discover_after_the_first_one_did(
    monkeypatch, tmp_path
):
    """A scraper reused across accounts must discover for each one.

    The guard against re-attempting a capture is per *profile*. As a single
    instance-wide flag it locked discovery out after the first target, so a
    batch scrape silently sent every account after the first down the slow DOM
    path — with nothing in the report to say the fast path had never even been
    tried for them.
    """
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    api = _api([_page(["AA1"], username="alice"), _page(["BB1"], username="bob")],
               monkeypatch, tmp_path)
    api._resolve_user_id = lambda _u: "999"
    api._fetch_posts_via_playwright = lambda _u, _l: []

    discovered = []

    def fake_discover_and_cache(username, cookies):
        discovered.append(username)
        return RequestTemplate(
            params={"doc_id": "D", "lsd": f"lsd-{username}"},
            variables=_captured_vars(userID="999"),
            username=username,
            captured_at=1.0,
        )

    monkeypatch.setattr("threadstractormf.api.discover_and_cache", fake_discover_and_cache)

    first = api.get_posts("alice", limit=5)
    second = api.get_posts("bob", limit=5)

    assert discovered == ["alice", "bob"], discovered
    assert len(first) == 1 and len(second) == 1
    # Neither may have degraded: the whole point is the fast path for both.
    assert api.last_report.used_fallback is False


def test_the_attempt_guard_still_stops_retrying_the_same_profile(
    monkeypatch, tmp_path
):
    """Per-profile must not become per-page: one failure, one attempt."""

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    api = _api([_page([])], monkeypatch, tmp_path)
    api._resolve_user_id = lambda _u: "999"
    api._fetch_posts_via_playwright = lambda _u, _l: []

    calls = {"n": 0}

    def fake_discover_and_cache(username, cookies):
        calls["n"] += 1
        raise RuntimeError("no query observed")

    monkeypatch.setattr("threadstractormf.api.discover_and_cache", fake_discover_and_cache)

    api.get_posts("alice", limit=None)
    first = calls["n"]
    assert first >= 1

    # A second scrape of the same account must not launch the browser again.
    api2 = _api([_page([])], monkeypatch, tmp_path)
    api2._resolve_user_id = lambda _u: "999"
    api2._fetch_posts_via_playwright = lambda _u, _l: []
    api2._template_attempted.add("alice")
    calls["n"] = 0
    api2.get_posts("alice", limit=5)
    assert calls["n"] == 0, "an already-attempted profile must not recapture"


def test_the_global_attempt_budget_still_bounds_a_batch_scrape(monkeypatch, tmp_path):
    """Per-profile guards must not add up to unbounded browser time."""

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    api = _api([_stale()], monkeypatch, tmp_path)
    api._resolve_user_id = lambda _u: "999"
    api._fetch_posts_via_playwright = lambda _u, _l: []

    calls = {"n": 0}

    def fake_discover_and_cache(username, cookies):
        calls["n"] += 1
        return RequestTemplate(
            params={"doc_id": "BAD", "lsd": "x"},
            variables=_captured_vars(userID="999"),
            username=username,
            captured_at=1.0,
        )

    monkeypatch.setattr("threadstractormf.api.discover_and_cache", fake_discover_and_cache)

    for name in ("a", "b", "c", "d", "e"):
        api.get_posts(name, limit=5)

    from threadstractormf.api import _MAX_TEMPLATE_ATTEMPTS

    assert calls["n"] <= _MAX_TEMPLATE_ATTEMPTS, calls


def test_a_batch_of_anonymous_profiles_does_not_claim_the_query_was_refused(
    capsys, monkeypatch, tmp_path
):
    """A batch with no session must not burn the attempt budget.

    The budget counts browser launches. Spending it on profiles that were never
    even attemptable — no session to capture from — made the third one warn
    that "the query was refused again after recapturing its id", which never
    happened.
    """
    import http.cookiejar as cj

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    api = ThreadsAPI(cj.CookieJar(), rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: None
    api._fetch_posts_via_playwright = lambda _u, _l: []

    # Each run's stderr is collected as it is produced. Draining with
    # capsys.readouterr() would reset the buffer, so the parts are kept and
    # joined below.
    parts = []
    for name in ("a", "b", "c", "d"):
        api.get_posts(name, limit=1)
        parts.append(capsys.readouterr().err)

    err = "".join(parts)
    assert api._template_attempts == 0, api._template_attempts
    assert "refused again" not in err, err
    assert "no session cookies" in err


def test_an_injected_client_is_never_discarded_when_the_template_arrives(
    monkeypatch, tmp_path
):
    """A caller-supplied transport must survive template discovery.

    Rebuilding the client to pick up the captured ``X-FB-LSD`` is how the
    impersonation crash was fixed. Doing it unconditionally is worse than the
    crash: it silently replaces a caller's MockTransport — or their own pooled
    session — with a fresh real one and sends the request to the network.
    """
    api = _api([_page(["ZZ1"])], monkeypatch, tmp_path)
    injected = api._client
    api._fetch_posts_via_playwright = lambda _u, _l: []

    # Start with no template, so discovery fills one in after the profile fetch
    # has already built the client.
    api._template = None
    monkeypatch.setattr(
        "threadstractormf.api.discover_and_cache",
        lambda u, c: RequestTemplate(params={"doc_id": "D", "lsd": "L"}, captured_at=1.0),
    )

    posts = api.get_posts("user", limit=5)

    assert posts, "the scrape should still work with an injected transport"
    assert api._client is injected, "an injected client must not be replaced"
    assert api._test_calls["n"] >= 1, "and it must have served the request"


class _HeaderlessClient:
    """Stands in for CurlClient, which exposes no ``headers`` attribute.

    ``--impersonate`` builds a CurlClient. The original fix reached into
    ``self._client.headers`` to refresh the lsd after discovery, which exists on
    httpx.Client but not here — an AttributeError on the path the README
    recommends for getting past Meta's WAF.
    """

    def __init__(self, pages):
        self.pages = pages
        self.n = 0
        self.closes = 0
        self.closed = False

    def request(self, _method, _url, **_kwargs):
        self.n += 1
        payload = self.pages[min(self.n - 1, len(self.pages) - 1)]
        resp = httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        # httpx only raises for status on a response that knows its request.
        resp.request = httpx.Request("POST", _url)
        return resp

    def close(self):
        self.closes += 1
        self.closed = True


def test_the_impersonation_client_has_no_headers_to_patch(monkeypatch, tmp_path):
    """Guards the assumption behind the fix, and the crash it removes.

    If CurlClient ever gains a mutable ``headers``, this test starts failing
    and the note in api.py can be revisited — but until then, any code that
    reaches for ``client.headers`` breaks --impersonate.
    """
    from threadstractormf._backend import CurlClient

    assert not hasattr(CurlClient, "headers")
    assert {"close", "request", "stream"} <= set(dir(CurlClient))


def test_the_client_is_rebuilt_once_per_walk_not_once_per_page(monkeypatch, tmp_path):
    """A 200-page crawl must not pay 200 TLS handshakes.

    The client carries X-FB-LSD from the template, and discovery runs after the
    profile fetch already built a client — so one rebuild is needed to pick the
    captured lsd up. It used to sit inside the page loop, which closed and
    rebuilt the connection on every single page: with `_owns_client` set on the
    new one each time, the condition held forever.
    """
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    pages = [_page([f"P{i}"], has_next=True, cursor=f"c{i}") for i in range(6)]

    api = ThreadsAPI(session_cookies(), rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    client = _HeaderlessClient(pages)
    def _fake_get_client():
        # Mirrors the real _get_client, which *stores* the client: the rebuild
        # guard reads self._client, so a mock that merely returns one would
        # leave it None and silently disable the check being tested.
        api._client = client
        return client

    monkeypatch.setattr(api, "_get_client", _fake_get_client)
    api._client = client
    api._owns_client = True  # as if this class had built it
    api._templates["user"] = _template(doc_id="D", lsd="L")
    api._fetch_posts_via_playwright = lambda _u, _l: []

    posts = api.get_posts("user", limit=None)

    assert posts, "the walk should have produced posts"
    # Exactly one rebuild: the client built for the profile fetch carries the
    # old lsd, and the template's lsd has to replace it. With the rebuild inside
    # the page loop this was one close per page, i.e. a fresh TLS handshake for
    # every request of a 200-page crawl.
    assert client.closes == 1, client.closes
    assert client.n >= len(pages), "every page must be served"


def test_an_injected_client_is_still_never_rebuilt(monkeypatch, tmp_path):
    """Rebuilding once per walk is still wrong for a client we do not own."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    pages = [_page(["P1"], has_next=True, cursor="c1"), _page(["P2"])]

    api = ThreadsAPI(session_cookies(), rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    client = _HeaderlessClient(pages)
    def _fake_get_client():
        # Mirrors the real _get_client, which *stores* the client: the rebuild
        # guard reads self._client, so a mock that merely returns one would
        # leave it None and silently disable the check being tested.
        api._client = client
        return client

    monkeypatch.setattr(api, "_get_client", _fake_get_client)
    api._client = client
    api._owns_client = False  # injected: not ours to touch
    api._templates["user"] = _template(doc_id="D", lsd="L")
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=None)

    assert api._client is client
    assert client.closes == 0, "an injected client must never be closed"
    assert client.n == 2


def test_a_headerless_client_still_gets_a_refreshed_lsd(monkeypatch, tmp_path):
    """The impersonation path end to end: no AttributeError, lsd applied.

    This is the shape of the real ``--impersonate`` flow: the client is built
    by ``_get_client`` (so the class owns it), and it is a CurlClient, which has
    no ``headers``. The original code reached for
    ``client.headers.update(...)`` to pick up the captured lsd and raised
    AttributeError here — on the path the README recommends for getting past
    Meta's WAF.
    """
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))
    api = ThreadsAPI(
        session_cookies(),
        impersonate="chrome131",  # -> CurlClient, which has no .headers
        rate_limit_config=rl.RateLimitConfig(enabled=False),
    )
    api._resolve_user_id = lambda _u: "999"
    api._template = None
    api._fetch_posts_via_playwright = lambda _u, _l: []

    # Stand in for the CurlClient that _get_client would build, and mark it as
    # owned so the refresh path actually runs.
    client = _HeaderlessClient([_page(["HH1", "HH2"])])
    def _fake_get_client():
        # Mirrors the real _get_client, which *stores* the client: the rebuild
        # guard reads self._client, so a mock that merely returns one would
        # leave it None and silently disable the check being tested.
        api._client = client
        return client

    monkeypatch.setattr(api, "_get_client", _fake_get_client)
    api._client = client
    api._owns_client = True

    monkeypatch.setattr(
        "threadstractormf.api.discover_and_cache",
        lambda u, c: RequestTemplate(
            params={"doc_id": "D", "lsd": "CAPTURED_LSD"}, captured_at=1.0
        ),
    )

    posts = api.get_posts("user", limit=5)

    assert len(posts) == 2, posts
    # Refreshed by replacement, not by patching a header dict it does not have.
    assert not client.closed or client.n >= 1
    assert api._headers()["X-FB-LSD"] == "CAPTURED_LSD"


def test_a_rejected_doc_id_is_recaptured_and_the_scrape_recovers(
    monkeypatch, tmp_path
):
    """The regression this design exists to prevent.

    A stale id must not end the scrape. The template is thrown away, a fresh one
    captured, and the walk retried — so the next rotation needs no release.
    """
    api = _api([_stale(), _page(["AA1", "AA2"])], monkeypatch, tmp_path)

    captured = {"n": 0}

    def fake_discover(username, cookies):
        captured["n"] += 1
        return RequestTemplate(
            params={"doc_id": "FRESH_ID", "lsd": "freshlsd"},
            variables=_captured_vars(userID="999"),
            username="user",
            captured_at=1.0,
        )

    monkeypatch.setattr("threadstractormf.api.discover_and_cache", fake_discover)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    posts = api.get_posts("user", limit=10)

    # Two captures: the first to get a starting point (there was no cache),
    # the second after the server refused the id that produced.
    assert captured["n"] == 2, captured
    assert len(posts) == 2, posts
    assert api.last_report.used_fallback is False
    assert api.last_report.api_error == ""


def test_the_stale_template_is_also_evicted_from_disk(monkeypatch, tmp_path):
    """Otherwise every later run would replay the dead id again.

    The real ``discover_and_cache`` is used here rather than a stub: persisting
    the replacement is part of what discovery does, and the point of this test
    is that the *cache* ends up holding the new id. The suite-wide
    ``no_network`` fixture stubs discovery out, so it is un-stubbed here.
    """
    import threadstractormf.api as api_module

    api = _api([_stale(), _page(["BB1"])], monkeypatch, tmp_path)
    fp = disc.session_fingerprint(session_cookies())
    save_template(_template(doc_id="DEAD", lsd="old", username="user"), fp, "user")

    monkeypatch.setattr(
        disc, "discover", lambda u, c: RequestTemplate(
            params={"doc_id": "NEW", "lsd": "n"},
            variables=_captured_vars(userID="999"),
            username="user",
            captured_at=1.0,
        )
    )
    monkeypatch.setattr(api_module, "discover_and_cache", discover_and_cache)
    api._fetch_posts_via_playwright = lambda _u, _l: []
    api.get_posts("user", limit=5)

    on_disk = load_template(fp, "user")
    assert on_disk is not None
    assert on_disk.doc_id == "NEW"


def test_recovery_is_bounded_so_a_dead_query_cannot_loop(monkeypatch, tmp_path):
    """A template that keeps failing must not hammer Threads.

    Two captures is the ceiling: one to obtain an id, one after the server
    refused it. Past that the problem is structural, not a rotation.
    """
    api = _api([_stale()], monkeypatch, tmp_path)

    captured = {"n": 0}

    def fake_discover(username, cookies):
        captured["n"] += 1
        return RequestTemplate(
            params={"doc_id": "STILL_BAD", "lsd": "x"},
            variables=_captured_vars(userID="999"),
            username="user",
            captured_at=1.0,
        )

    monkeypatch.setattr("threadstractormf.api.discover_and_cache", fake_discover)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=5)

    assert captured["n"] == 2, captured
    # The failure is still reported, and the DOM fallback still runs.
    assert api.last_report.api_error
    assert api.last_report.used_fallback is True


def test_without_a_browser_the_run_degrades_with_an_actionable_message(
    monkeypatch, tmp_path, capsys
):
    """No cache and no browser: fail fast and say how to fix it.

    Playwright is an optional extra, so its absence must not be a hard error,
    and the message must name the install command rather than surfacing a bare
    ImportError from deep inside the scrape.
    """
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))

    def no_playwright(*_a, **_kw):
        raise ImportError("no playwright")

    monkeypatch.setattr("threadstractormf.api._require_playwright", no_playwright)

    api = _api([_page(["CC1"])], monkeypatch, tmp_path)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    api.get_posts("user", limit=5)

    err = capsys.readouterr().err
    assert "threadstractormf[browser]" in err
    assert api.last_report.used_fallback is True


def test_discovery_survives_a_missing_browser_module(monkeypatch, tmp_path):
    """discover_and_cache raising must not escape as an unhandled crash."""
    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))

    def boom(*_a, **_kw):
        raise RuntimeError("no query observed")

    monkeypatch.setattr(disc, "discover", boom)
    try:
        discover_and_cache("user", None)
    except RuntimeError as exc:
        assert "no query observed" in str(exc)
    else:  # pragma: no cover - the stub always raises
        raise AssertionError("expected RuntimeError")
