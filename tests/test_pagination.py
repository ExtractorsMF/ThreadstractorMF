"""Regression: the GraphQL client must paginate instead of truncating.

The BarcelonaProfileMediaTabRefetchableDirectQuery endpoint only ever returns
one page per request (`first`), so a single request capped every scrape at
_PAGE_SIZE posts: `--limit 50` returned 12 with no warning, and because the
partial result was non-empty the Playwright fallback never kicked in either.
"""

import json

import httpx

import threadstractormf.rate_limit as rl
from threadstractormf.api import _MAX_PAGES, _PAGE_SIZE, ThreadsAPI

_IMAGE = {"candidates": [{"width": 1080, "url": "https://scontent.cdninstagram.com/v/t51.1/a.jpg"}]}


def _page(codes, *, has_next=False, cursor=None, include_page_info=True):
    edges = [
        {
            "node": {
                "thread_items": [
                    {
                        "post": {
                            "pk": str(1000 + i),
                            "code": code,
                            "user": {"username": "user"},
                            "caption": {"text": "t"},
                            "taken_at": 1700000000,
                            "image_versions2": _IMAGE,
                        }
                    }
                ]
            }
        }
        for i, code in enumerate(codes)
    ]
    media = {"edges": edges}
    if include_page_info:
        media["page_info"] = {"has_next_page": has_next, "end_cursor": cursor}
    return {"data": {"mediaData": media}}


def _scrape(pages, limit):
    """Drive get_posts over a mocked transport; returns (posts, request_count)."""
    calls = {"n": 0, "afters": []}

    def handler(request):
        i = calls["n"]
        calls["n"] += 1
        body = request.content.decode()
        import urllib.parse

        calls["afters"].append(json.loads(urllib.parse.parse_qs(body)["variables"][0])["after"])
        # content_type matters: get_posts now checks it to tell a real GraphQL
        # payload from the HTML shell Threads serves when it rejects the query,
        # and a bare httpx.Response defaults to no content-type at all.
        return httpx.Response(
            200,
            content=json.dumps(pages[min(i, len(pages) - 1)]).encode(),
            headers={"content-type": "application/json"},
        )

    api = ThreadsAPI({"csrftoken": "x"}, rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._resolve_user_id = lambda _u: "999"
    # get_posts falls back to the DOM scraper whenever GraphQL yields nothing
    # (api.py:923). Unstubbed, that launches a real Chromium and hits the
    # network, so a legitimately empty profile made these tests pass by
    # accident and fail on a runner with no browsers installed. The other
    # test modules stub this the same way.
    api._fetch_posts_via_playwright = lambda _u, _l: []
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    return api.get_posts("user", limit=limit), calls


def _codes(n, prefix="P"):
    return [f"{prefix}{i}" for i in range(n)]


def test_single_page_when_no_more_results():
    posts, calls = _scrape([_page(_codes(5), has_next=False)], 50)
    assert len(posts) == 5
    assert calls["n"] == 1


def test_follows_the_cursor_until_the_limit():
    pages = [_page(_codes(12, prefix=f"p{i}_"), has_next=True, cursor=f"c{i}") for i in range(5)]
    posts, calls = _scrape(pages, 50)
    assert len(posts) == 50  # previously capped at 12 (one page)
    assert calls["n"] == 5
    assert calls["afters"] == [None, "c0", "c1", "c2", "c3"]


def test_limit_reached_mid_pagination_stops_early():
    pages = [_page(_codes(12, prefix=f"p{i}_"), has_next=True, cursor=f"c{i}") for i in range(9)]
    posts, calls = _scrape(pages, 25)
    assert len(posts) == 25
    assert calls["n"] == 3  # 12 + 12 + 1


def test_limit_larger_than_profile_returns_everything():
    # Last page is terminal, as a real profile's final page would be.
    pages = [
        _page(_codes(12, prefix=f"p{i}_"), has_next=i < 2, cursor=f"c{i}") for i in range(3)
    ]
    posts, calls = _scrape(pages, 500)
    assert len(posts) == 36
    assert calls["n"] == 3


def test_repeated_cursor_stops_instead_of_looping_forever():
    # Every page advertises the same end_cursor: page 1 sets it, page 2 repeats
    # it, and the walk must bail rather than spin.
    pages = [
        _page(_codes(12, prefix=f"p{i}_"), has_next=True, cursor="SAME") for i in range(2)
    ]
    posts, calls = _scrape(pages, 500)
    assert calls["n"] == 2
    assert len(posts) == 24  # both pages contributed; the walk then stopped


def test_missing_page_info_does_not_hang():
    """A renamed/removed page_info must degrade to a single page, not spin."""
    posts, calls = _scrape([_page(_codes(4), include_page_info=False)], 500)
    assert calls["n"] == 1
    assert len(posts) == 4


def test_null_page_info_does_not_hang():
    page = _page(_codes(4), has_next=True, cursor="x")
    page["data"]["mediaData"]["page_info"] = None
    posts, calls = _scrape([page], 500)
    assert calls["n"] == 1


def test_null_end_cursor_does_not_hang():
    page = _page(_codes(4), has_next=True, cursor=None)
    posts, calls = _scrape([page], 500)
    assert calls["n"] == 1


def test_duplicate_posts_across_pages_are_deduplicated():
    pages = [
        _page(_codes(12), has_next=True, cursor="c1"),
        _page(_codes(12), has_next=True, cursor="c2"),
    ]
    posts, _ = _scrape(pages, 500)
    assert len(posts) == 12
    assert len({p.id for p in posts}) == 12


def test_absent_media_data_stops_pagination():
    posts, calls = _scrape([{"data": {"mediaData": None}}, _page(_codes(3))], 500)
    assert calls["n"] == 1
    assert posts == []


def test_pagination_is_bounded_by_max_pages():
    """Even a server that always claims another page cannot loop forever."""
    pages = [
        _page(_codes(12, prefix=f"p{i}_"), has_next=True, cursor=f"c{i}")
        for i in range(_MAX_PAGES + 40)
    ]
    posts, calls = _scrape(pages, 10**6)
    assert calls["n"] <= _MAX_PAGES
    assert len(posts) <= _PAGE_SIZE * _MAX_PAGES


def test_reposts_are_filtered_across_all_pages():
    def page_with_repost(i):
        page = _page(_codes(12, prefix=f"p{i}_"), has_next=True, cursor=f"c{i}")
        # one edge belongs to somebody else
        page["data"]["mediaData"]["edges"][0]["node"]["thread_items"][0]["post"]["user"][
            "username"
        ] = "someone_else"
        return page

    pages = [page_with_repost(0), page_with_repost(1)]
    posts, _ = _scrape(pages, 500)
    assert posts
    assert all(p.username == "user" for p in posts)
