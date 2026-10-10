"""Regression: user-id resolution must not confuse a post id with an account.

``pk`` is a generic primary key — the project's own ``_parse_post_node`` treats
it as a *post* id. The profile page embeds the account's pk as well, so a
fallback matching the first ``"pk":"(\\d+)"`` in the document returned whichever
came first, usually a post, and handed it to GraphQL as ``userID``. The result
was an empty response and a silent fall back to the much slower DOM scraper,
with nothing shown to the user to explain why.
"""

import http.cookiejar
import json

import httpx
import pytest
from conftest import seed_template, session_cookies

import threadstractormf.rate_limit as rl
from threadstractormf.api import ThreadsAPI, _parse_profile_html

USER_ID = "25025320"
POST_PK = "3141592653589793271"
LSD = "AVqbzTOKEN"

# The account's data, then two posts each carrying their own pk plus a nested
# copy of the author's pk — four "pk" values, only two of them the account's.
# Real Threads HTML escapes separators as & and slashes as \/ inside JSON;
# both must survive the parser intact.
PROFILE_HTML = (
    f'<script>{{"userID":"{USER_ID}","lsd":"{LSD}",'
    '"profile_pic_url":"https:\\/\\/scontent.cdninstagram.com\\/a.jpg\\u0026x=1"}</script>'
    f'{{"pk":"{POST_PK}","code":"DP1","user":{{"pk":"{USER_ID}","username":"u"}}}}'
    f'{{"pk":"3141592653589793272","code":"DP2","user":{{"pk":"{USER_ID}"}}}}'
)

HTML_WITHOUT_USERID = (
    f'{{"pk":"{POST_PK}","code":"DP1","user":{{"pk":"{USER_ID}","username":"u"}}}}'
)


def _api(handler, *, username="u", seed=True, cookies="session", **cfg):
    if cookies is not None:
        cookie_jar = session_cookies() if cookies == "session" else cookies
    else:
        # Sin sesion: un jar que parece anonimo (sin sessionid ni ds_user_id).
        cookie_jar = http.cookiejar.CookieJar()
    api = ThreadsAPI(cookie_jar, rate_limit_config=rl.RateLimitConfig(enabled=False, **cfg))
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    if seed:
        seed_template(api, username=username)
    return api


# --- the parser -------------------------------------------------------------


def test_parses_all_three_values_in_one_pass():
    user_id, lsd, pic, _rev = _parse_profile_html(PROFILE_HTML)
    assert user_id == USER_ID
    assert lsd == LSD
    assert pic == "https://scontent.cdninstagram.com/a.jpg&x=1"


def test_profile_pic_url_is_fully_decoded():
    """\\u0026 and \\/ both have to go: a URL with either left in is unservable."""
    _uid, _lsd, pic, _rev = _parse_profile_html(PROFILE_HTML)
    assert "\\u0026" not in pic
    assert "\\/" not in pic
    assert "scontent.cdninstagram.com" in pic
    assert "&x=1" in pic


def test_parser_ignores_the_posts_pk():
    assert _parse_profile_html(PROFILE_HTML)[0] != POST_PK


def test_parser_returns_none_without_userid():
    """The regression: it must NOT fall back to a pk, which is a post id here."""
    assert _parse_profile_html(HTML_WITHOUT_USERID) == (None, None, None, None)


def test_parser_on_empty_input():
    assert _parse_profile_html("") == (None, None, None, None)
    assert _parse_profile_html("<html>nothing here</html>") == (None, None, None, None)


def test_parser_tolerates_escaped_spacing():
    html = '{ "userID" : "42" , "lsd" : "T" }'
    assert _parse_profile_html(html)[:2] == ("42", "T")


# --- resolution -------------------------------------------------------------


def test_resolve_user_id_returns_the_account_id():
    api = _api(lambda _r: httpx.Response(200, text=PROFILE_HTML))
    assert api._resolve_user_id("u") == USER_ID


def test_resolve_user_id_raises_instead_of_using_a_post_pk():
    api = _api(lambda _r: httpx.Response(200, text=HTML_WITHOUT_USERID))
    with pytest.raises(ValueError, match="could not resolve userID"):
        api._resolve_user_id("u")
    assert api._user_id_cache == {}, "nothing may be cached from a failed lookup"


def test_resolve_user_id_also_captures_lsd():
    api = _api(lambda _r: httpx.Response(200, text=PROFILE_HTML))
    api._resolve_user_id("u")
    assert api._lsd == LSD


def test_resolve_user_id_is_cached():
    calls = {"n": 0}

    def handler(_r):
        calls["n"] += 1
        return httpx.Response(200, text=PROFILE_HTML)

    api = _api(handler)
    api._resolve_user_id("u")
    api._resolve_user_id("u")
    assert calls["n"] == 1


# --- one page fetch for profile + posts ------------------------------------


def test_profile_and_posts_share_a_single_page_fetch():
    """The CLI calls get_profile then get_posts; that used to be two GETs."""
    calls = {"profile": 0, "graphql": 0}

    def handler(request):
        if "graphql" in str(request.url):
            calls["graphql"] += 1
            return httpx.Response(200, content=json.dumps({"data": {"mediaData": None}}).encode())
        calls["profile"] += 1
        return httpx.Response(200, text=PROFILE_HTML)

    api = _api(handler)
    api._fetch_posts_via_playwright = lambda _u, _l: []

    profile = api.get_profile("u")
    assert profile.user_id == USER_ID
    api.get_posts("u", limit=5)

    assert calls["profile"] == 1, "the profile page is fetched once and cached"
    assert calls["graphql"] == 1


def test_get_profile_reports_the_account_id():
    api = _api(lambda _r: httpx.Response(200, text=PROFILE_HTML))
    profile = api.get_profile("u")
    assert profile.user_id == USER_ID
    assert profile.profile_pic_url is not None
    assert profile.profile_pic_media is not None
    assert profile.profile_pic_media.id == "u_profile"
    assert profile.profile_pic_media.post_id == "u_profile"


def test_get_profile_seeds_the_user_id_cache():
    api = _api(lambda _r: httpx.Response(200, text=PROFILE_HTML))
    api.get_profile("u")
    assert api._user_id_cache["u"] == USER_ID


def test_get_profile_survives_an_unreachable_page():
    api = _api(lambda _r: (_ for _ in ()).throw(httpx.ConnectError("down")))
    api._fetch_profile_pic_via_playwright = lambda _u: None
    profile = api.get_profile("u")
    assert profile.user_id is None
    assert profile.profile_pic_url is None


def test_get_posts_falls_back_without_a_resolvable_id():
    api = _api(lambda _r: httpx.Response(200, text=HTML_WITHOUT_USERID))
    api._fetch_posts_via_playwright = lambda _u, _l: ["DOM"]
    # No userID -> straight to the DOM scraper, without a doomed GraphQL call.
    assert api.get_posts("u", limit=5) == ["DOM"]


def test_anonymous_scraping_says_why_the_api_path_is_unavailable(capsys, monkeypatch, tmp_path):
    """A profile page with no userID and no session: explain, then fall back.

    Threads renders the profile client-side, so an unauthenticated fetch
    carries no ``userID`` at all, and an anonymous browser never issues the
    media query in the first place — only fetchBarcelonaExperimentQuery. So
    there is nothing to capture, and the captured tokens could not be replayed
    either even if there were.

    What matters is that this is *said* rather than silent: the DOM path still
    works anonymously, and the message names how to get the fast one.
    """
    import threadstractormf._discovery as disc

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))

    api = _api(lambda _r: httpx.Response(200, text=HTML_WITHOUT_USERID),
               username="u", seed=False, cookies=None)
    api._fetch_posts_via_playwright = lambda _u, _l: ["DOM"]

    assert api.get_posts("u", limit=5) == ["DOM"]
    assert api.last_report.used_fallback is True
    err = capsys.readouterr().err
    assert "no session cookies" in err
    assert "--cookies" in err, "the message must say how to get the fast path"


def test_get_posts_still_falls_back_when_nothing_can_resolve_the_account(
    monkeypatch, tmp_path
):
    """With neither an HTML id nor a usable template, the DOM path is all
    that is left — and it must be reached rather than raising."""
    import threadstractormf._discovery as disc

    monkeypatch.setattr(disc, "_CACHE_SUBDIR", str(tmp_path / "cache"))

    api = _api(lambda _r: httpx.Response(200, text=HTML_WITHOUT_USERID), username="u", seed=False)
    api._fetch_posts_via_playwright = lambda _u, _l: ["DOM"]

    monkeypatch.setattr(
        "threadstractormf.api.discover_and_cache",
        lambda user, cookies: (_ for _ in ()).throw(RuntimeError("no query observed")),
    )

    assert api.get_posts("u", limit=5) == ["DOM"]
    assert api.last_report.used_fallback is True
