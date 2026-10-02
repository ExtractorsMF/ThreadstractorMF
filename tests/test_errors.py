"""Regression: failures must tell the user what to do about them.

Most of these paths used to swallow the reason: a JSON cookie export raised a
bare traceback, a missing Playwright produced ``ImportError: No module named
'playwright'``, and falling back to the DOM scraper happened silently, so a
blocked WAF looked like "it is just slow".
"""

import http.cookiejar
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import threadstractormf.rate_limit as rl
from threadstractormf.api import ThreadsAPI, _require_playwright
from threadstractormf.auth import ensure_netscape_header, load_netscape_cookies

CLI = [sys.executable, "-m", "threadstractormf.cli"]


# --- cookies: the most common mistake ---------------------------------------


def _json_cookies(tmp_path: Path) -> Path:
    f = tmp_path / "cookies.json"
    f.write_text('[{"name":"sessionid","value":"x","domain":".threads.com"}]')
    return f


def test_json_cookie_export_gets_the_actionable_message(tmp_path):
    with pytest.raises(http.cookiejar.LoadError) as excinfo:
        load_netscape_cookies(_json_cookies(tmp_path))
    message = str(excinfo.value)
    assert "Get cookies.txt LOCALLY" in message
    assert "not JSON" in message


def test_header_validator_itself_is_actionable(tmp_path):
    with pytest.raises(http.cookiejar.LoadError, match="Netscape HTTP Cookie File"):
        ensure_netscape_header(_json_cookies(tmp_path))


def test_missing_file_still_reports_the_path(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(FileNotFoundError, match="nope.txt"):
        load_netscape_cookies(missing)


def test_valid_netscape_file_is_unaffected(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(
        "# Netscape HTTP Cookie File\n"
        ".threads.net\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\tabc\n"
    )
    jar = load_netscape_cookies(f)
    assert {c.name for c in jar} == {"csrftoken"}


def test_cli_reports_bad_cookies_without_a_traceback(tmp_path):
    result = subprocess.run(
        CLI + ["--cookies", str(_json_cookies(tmp_path)), "-d", str(tmp_path / "dl"), "@u"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, "a bad cookie file must not print a stack"
    # Rich wraps long lines, so match on a fragment that survives wrapping.
    assert "Get cookies.txt" in combined and "not JSON" in combined


def test_cli_reports_a_missing_file_cleanly(tmp_path):
    result = subprocess.run(
        CLI + ["--cookies", str(tmp_path / "nope.txt"), "-d", str(tmp_path / "dl"), "@u"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stdout + result.stderr
    assert "not found" in result.stdout + result.stderr


# --- playwright -------------------------------------------------------------


def test_require_playwright_is_a_noop_when_installed():
    _require_playwright()  # must not raise; the extra is installed here


def test_require_playwright_explains_the_install(monkeypatch):
    """With the extra missing, the message must name both install steps."""
    import importlib.abc

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.startswith("playwright"):
                # Raise from find_spec so the import statement itself fails,
                # which is what _require_playwright() has to catch.
                raise ImportError("No module named 'playwright'")

    for name in [m for m in sys.modules if m.startswith("playwright")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [Block(), *sys.meta_path])

    with pytest.raises(ImportError) as excinfo:
        _require_playwright()
    message = str(excinfo.value)
    assert "threadstractormf[browser]" in message
    assert "playwright install chromium" in message


# --- fallback diagnostics ---------------------------------------------------


def _api(handler):
    api = ThreadsAPI({"csrftoken": "x"}, rate_limit_config=rl.RateLimitConfig(enabled=False))
    api._client = httpx.Client(transport=httpx.MockTransport(handler))
    return api


def test_falling_back_to_the_dom_scraper_is_announced(capsys):
    """The GraphQL path raised, so the DOM scraper takes over."""

    def handler(request):
        if "graphql" in str(request.url):
            raise httpx.ConnectError("network down")
        return httpx.Response(200, text='{"userID":"1"}')

    api = _api(handler)
    api._fetch_posts_via_playwright = lambda _u, _l: ["DOM"]
    assert api.get_posts("u", limit=5) == ["DOM"]
    err = capsys.readouterr().err
    assert "Playwright" in err
    assert "slower" in err
    # The DOM scraper returned posts, so there is nothing else to report.
    assert "no posts found" not in err


def test_an_empty_result_is_reported(capsys):
    """Both the fallback and the empty outcome are worth saying out loud."""

    def handler(request):
        if "graphql" in str(request.url):
            raise httpx.ConnectError("network down")
        return httpx.Response(200, text='{"userID":"1"}')

    api = _api(handler)
    api._fetch_posts_via_playwright = lambda _u, _l: []
    api.get_posts("u", limit=5)
    err = capsys.readouterr().err
    assert "Playwright" in err, "the fallback itself"
    assert "no posts found" in err, "the empty outcome"
    assert "private" in err


def test_an_empty_result_is_a_warning_not_an_exception(capsys):
    """An empty or private profile legitimately yields nothing; raising would be
    worse than saying so."""
    api = _api(lambda _r: httpx.Response(500))
    api._resolve_user_id = lambda _u: (_ for _ in ()).throw(ValueError("no id"))
    api._fetch_posts_via_playwright = lambda _u, _l: []
    assert api.get_posts("u", limit=5) == []


def test_a_successful_fast_path_stays_silent(capsys):
    """No noise when GraphQL works: the common case must stay quiet."""

    def handler(request):
        if "graphql" in str(request.url):
            return httpx.Response(200, content=b'{"data":{"mediaData":null}}')
        return httpx.Response(200, text='{"userID":"1"}')

    api = _api(handler)
    api._fetch_posts_via_playwright = lambda _u, _l: ["DOM"]
    api.get_posts("u", limit=5)
    err = capsys.readouterr().err
    assert "Playwright" not in err


def test_early_pagination_stop_is_announced(capsys):
    import json

    calls = {"n": 0}

    def handler(request):
        if "graphql" in str(request.url):
            calls["n"] += 1
            if calls["n"] == 1:
                page = {
                    "data": {
                        "mediaData": {
                            "edges": [
                                {
                                    "node": {
                                        "thread_items": [
                                            {
                                                "post": {
                                                    "pk": "1",
                                                    "code": "P1",
                                                    "user": {"username": "u"},
                                                    "taken_at": 1,
                                                    "image_versions2": {
                                                        "candidates": [
                                                            {
                                                                "url": "https://scontent.cdninstagram.com/v/t51.1/a.jpg"
                                                            }
                                                        ]
                                                    },
                                                }
                                            }
                                        ]
                                    }
                                }
                            ],
                            "page_info": {"has_next_page": True, "end_cursor": "c"},
                        }
                    }
                }
                return httpx.Response(200, content=json.dumps(page).encode())
            raise httpx.ConnectError("page 2 dropped")
        return httpx.Response(200, text='{"userID":"1"}')

    api = _api(handler)
    posts = api.get_posts("u", limit=None)
    assert posts, "the post found before the failure is returned"
    assert "pagination stopped early" in capsys.readouterr().err


# --- stdout is a contract ---------------------------------------------------


def test_diagnostics_go_to_stderr_not_stdout(capsys):
    """scrapmf parses stdout as one line per downloaded file."""
    api = _api(lambda _r: httpx.Response(500))
    api._resolve_user_id = lambda _u: (_ for _ in ()).throw(ValueError("no id"))
    api._fetch_posts_via_playwright = lambda _u, _l: []
    api.get_posts("u", limit=5)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err != ""
