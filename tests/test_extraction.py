"""Regression: media extraction quality and cookie handling.

Three independent defects that all produced wrong output silently:

- ``_guess_ext`` searched the whole URL for the substring "video", so an image
  at ".../video_cover.jpg" was written out with a .mp4 extension.
- ``video_versions[0]`` was taken as the rendition to download. Instagram orders
  those ascending by bitrate, so index 0 is the *worst* quality available.
- ``Threadscraper(cookies={...})`` stored the dict untouched, so the Playwright
  cookie converter received a mapping, produced an empty cookie list, and the
  DOM scraper ran with no session at all.
"""

import http.cookiejar
import sys
from unittest import mock

import pytest

from threadstractormf._backend import to_playwright_cookies
from threadstractormf.api import _guess_ext, _parse_post_node, _pick_best_video
from threadstractormf.auth import load_cookies_dict, load_from_browser
from threadstractormf.client import Threadscraper

# --- _guess_ext -------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # the regression: "video" in the path is not an mp4
        ("https://cdn/v/t51.2885-19/video_cover.jpg?_nc_cat=1", "jpg"),
        ("https://cdn/v/t51.1/reel_video_cover.png", "png"),
        ("https://cdn/v/t51.1/a.jpg", "jpg"),
        ("https://cdn/v/t51.1/a.jpeg", "jpeg"),
        ("https://cdn/v/t51.1/a.webp", "webp"),
        ("https://cdn/v/t51.1/a.avif", "avif"),
        ("https://cdn/v/t51.1/a.gif", "gif"),
        ("https://cdn/o1/v/t16/f2/m86/A.mp4?_nc_cat=101", "mp4"),
        # formats that used to fall through to "jpg"
        ("https://cdn/v/t65/x.webm", "webm"),
        ("https://cdn/v/t65/x.mov", "mov"),
        # no extension in the path at all
        ("https://cdn/v/t51.1/stream", "jpg"),
    ],
)
def test_guess_ext_reads_the_path(url, expected):
    assert _guess_ext(url) == expected


def test_guess_ext_keeps_jpg_not_jpeg():
    """Folding .jpg into .jpeg would rename existing files and break
    scrapmf's filename-dedup archive."""
    assert _guess_ext("https://cdn/a.jpg") == "jpg"
    assert _guess_ext("https://cdn/a.jpeg") == "jpeg"


def test_guess_ext_ignores_query_string_for_extension():
    assert _guess_ext("https://cdn/a.jpg?video=1&t=mp4") == "jpg"


# --- _pick_best_video -------------------------------------------------------

_LADDER = [
    {"type": 101, "width": 240, "height": 426, "url": "https://cdn/baja.mp4"},
    {"type": 102, "width": 480, "height": 852, "url": "https://cdn/media.mp4"},
    {"type": 103, "width": 720, "height": 1280, "url": "https://cdn/alta.mp4"},
]


def test_picks_highest_rendition_not_index_zero():
    assert _pick_best_video(_LADDER) == "https://cdn/alta.mp4"
    assert _pick_best_video(_LADDER) != _LADDER[0]["url"]


def test_falls_back_to_last_when_no_dimensions():
    """Without width/height the ordering is all we have to go on."""
    versions = [{"url": "a"}, {"url": "b"}, {"url": "c"}]
    assert _pick_best_video(versions) == "c"


def test_single_version_is_returned():
    assert _pick_best_video([{"url": "solo.mp4"}]) == "solo.mp4"


@pytest.mark.parametrize(
    "versions", [[], None, "nope", [{}], [{"width": 1}], [{"url": None}]]
)
def test_unusable_inputs_return_none(versions):
    assert _pick_best_video(versions) is None


def test_parse_post_node_uses_best_video_for_single_media():
    post = _parse_post_node(
        {
            "thread_items": [
                {
                    "post": {
                        "pk": "1",
                        "code": "ABC",
                        "user": {"username": "user"},
                        "taken_at": 1700000000,
                        "video_versions": _LADDER,
                    }
                }
            ]
        }
    )
    assert post is not None
    assert post.media[0].type == "video"
    assert post.media[0].url == "https://cdn/alta.mp4"


def test_parse_post_node_uses_best_video_inside_a_carousel():
    post = _parse_post_node(
        {
            "thread_items": [
                {
                    "post": {
                        "pk": "2",
                        "code": "XYZ",
                        "user": {"username": "user"},
                        "taken_at": 1700000000,
                        "carousel_media": [
                            {"video_versions": _LADDER},
                            {
                                "image_versions2": {
                                    "candidates": [
                                        {"width": 320, "url": "https://cdn/small.jpg"},
                                        {"width": 1080, "url": "https://cdn/big.jpg"},
                                    ]
                                }
                            },
                        ],
                    }
                }
            ]
        }
    )
    assert post is not None
    by_id = {m.id: m for m in post.media}
    assert by_id["XYZ_1"].url == "https://cdn/alta.mp4"  # video: best rendition
    assert by_id["XYZ_2"].url == "https://cdn/big.jpg"  # image: widest


# --- dict cookies -----------------------------------------------------------


def test_dict_cookies_become_a_usable_jar():
    scraper = Threadscraper(cookies={"sessionid": "abc", "csrftoken": "def"})
    try:
        assert isinstance(scraper.cookies, http.cookiejar.CookieJar)
        assert scraper.api._headers()["X-CSRFToken"] == "def"
    finally:
        scraper.close()


def test_dict_cookies_reach_playwright():
    """The regression: an empty list here means the DOM scraper is anonymous."""
    scraper = Threadscraper(cookies={"sessionid": "abc", "csrftoken": "def"})
    try:
        cookies = to_playwright_cookies(scraper.cookies)
        assert {c["name"] for c in cookies} == {"sessionid", "csrftoken"}
        # and they must be domain-scoped so Chromium sends them to www.
        assert all(c["domain"].startswith(".") for c in cookies)
    finally:
        scraper.close()


def test_load_cookies_dict_scopes_to_threads_subdomains():
    jar = load_cookies_dict({"sessionid": "x"}, domain=".threads.net")
    cookie = next(iter(jar))
    assert cookie.domain == ".threads.net"
    assert cookie.domain_initial_dot is True
    # it must be visible from www.threads.net
    assert to_playwright_cookies(jar)[0]["domain"] == ".threads.net"


# --- load_from_browser ------------------------------------------------------


class _FakeBrowserCookie3:
    """Stand-in that records which domains were asked for."""

    def __init__(self):
        self.requested: list[str] = []

    def _jar(self, domain_name=None):
        self.requested.append(domain_name)
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(
            http.cookiejar.Cookie(
                0,
                f"c_{domain_name.split('.')[0]}",
                "v",
                None,
                False,
                domain_name,
                True,
                True,
                "/",
                True,
                True,
                None,
                False,
                None,
                None,
                None,
                {},
            )
        )
        return jar

    brave = chrome = chromium = firefox = edge = opera = vivaldi = _jar


@pytest.fixture
def fake_bc():
    fake = _FakeBrowserCookie3()
    with mock.patch.dict(sys.modules, {"browser_cookie3": fake}):
        yield fake


@pytest.mark.parametrize(
    "browser", ["brave", "chrome", "firefox", "edge", "chromium", "opera", "vivaldi"]
)
def test_every_browser_gets_both_domains(fake_bc, browser):
    """Only Brave used to receive the threads.net half, silently leaving
    Chrome/Firefox/Edge with an incomplete session."""
    jar = load_from_browser(browser)
    domains = {c.domain for c in jar}
    assert domains == {"threads.com", "threads.net"}
    assert fake_bc.requested == ["threads.com", "threads.net"]


def test_unknown_browser_raises_listing_valid_names(fake_bc):
    with pytest.raises(ValueError, match="unsupported browser"):
        load_from_browser("netscape")


def test_brave_browser_alias_still_works(fake_bc):
    assert {c.domain for c in load_from_browser("Brave-Browser")} == {
        "threads.com",
        "threads.net",
    }
