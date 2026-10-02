"""Regression: shared retry classification and the Playwright cookie converter.

Both backends (httpx by default, curl_cffi under --impersonate) must agree on
what counts as a transient failure, and cookie domains handed to Playwright must
keep their leading dot.
"""

import http.cookiejar

import httpx
import pytest

from threadstractormf._backend import (
    is_retryable_status,
    is_transient_error,
    to_playwright_cookies,
)


def _status_error(exc_cls, code):
    resp = httpx.Response(code, request=httpx.Request("GET", "https://cdn/x.mp4"))
    return exc_cls("boom", request=resp.request, response=resp)


# --- is_retryable_status ----------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retryable_statuses(status):
    assert is_retryable_status(status)


@pytest.mark.parametrize("status", [200, 301, 400, 401, 403, 404, 429 - 1])
def test_permanent_statuses_are_not_retryable(status):
    assert not is_retryable_status(status)


# --- httpx family -----------------------------------------------------------


def test_httpx_transport_errors_are_transient():
    assert is_transient_error(httpx.ConnectError("[Errno -3] gaierror"))
    assert is_transient_error(httpx.ConnectTimeout("t"))
    assert is_transient_error(httpx.ReadTimeout("t"))
    assert is_transient_error(httpx.RemoteProtocolError("reset"))


def test_httpx_status_errors_follow_the_status_rule():
    assert is_transient_error(_status_error(httpx.HTTPStatusError, 503))
    assert is_transient_error(_status_error(httpx.HTTPStatusError, 429))
    assert not is_transient_error(_status_error(httpx.HTTPStatusError, 403))
    assert not is_transient_error(_status_error(httpx.HTTPStatusError, 404))


def test_non_transport_programming_errors_are_not_transient():
    assert not is_transient_error(ValueError("bad url"))
    assert not is_transient_error(httpx.InvalidURL("nope"))


# --- curl_cffi family -------------------------------------------------------
#
# curl_cffi raises its own exception classes when --impersonate is active, and
# its RequestException subclasses OSError. If the classifier tested OSError
# first it would treat every curl error as a plain OSError with no errno, and
# silently stop retrying.


def test_curl_errors_are_transient():
    curl_exc = pytest.importorskip("curl_cffi.requests.exceptions")
    assert is_transient_error(curl_exc.ReadTimeout("t"))
    assert is_transient_error(curl_exc.ConnectTimeout("t"))
    assert is_transient_error(curl_exc.DNSError("dns"))
    assert is_transient_error(curl_exc.SSLError("tls"))


def test_curl_request_exception_is_an_oserror_trap():
    curl_exc = pytest.importorskip("curl_cffi.requests.exceptions")
    # This is the exact trap: it IS an OSError, so ordering matters.
    assert issubclass(curl_exc.RequestException, OSError)
    # A timeout must still be recognised as transient despite the OSError base.
    assert is_transient_error(curl_exc.ReadTimeout("t"))


def test_curl_permanent_errors_are_not_transient():
    curl_exc = pytest.importorskip("curl_cffi.requests.exceptions")
    assert not is_transient_error(curl_exc.InvalidURL("nope"))
    assert not is_transient_error(curl_exc.TooManyRedirects("loop"))
    # RequestException with no response attached is not a retryable status.
    assert not is_transient_error(curl_exc.HTTPError("no response"))


def test_curl_status_error_follows_the_status_rule():
    curl_exc = pytest.importorskip("curl_cffi.requests.exceptions")
    resp503 = httpx.Response(503, request=httpx.Request("GET", "https://x"))
    resp403 = httpx.Response(403, request=httpx.Request("GET", "https://x"))
    assert is_transient_error(curl_exc.HTTPError("e", 0, resp503))
    assert not is_transient_error(curl_exc.HTTPError("e", 0, resp403))


# --- Playwright cookie conversion -------------------------------------------


def _jar(*domain_names: str) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for i, domain in enumerate(domain_names):
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=f"c{i}",
                value="v",
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path="/",
                path_specified=True,
                secure=True,
                expires=None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def test_playwright_cookies_keep_the_leading_dot():
    """Chromium treats a dotless domain as host-only and sends nothing to the
    www. subdomain, which logged the browser out entirely."""
    cookies = to_playwright_cookies(_jar(".threads.net"))
    assert cookies[0]["domain"] == ".threads.net"
    assert cookies[0]["domain"].startswith(".")


def test_playwright_cookies_are_never_dot_stripped():
    cookies = to_playwright_cookies(_jar(".threads.com", ".threads.net"))
    assert {c["domain"] for c in cookies} == {".threads.com", ".threads.net"}


def test_playwright_cookies_skip_domainless_entries():
    # Host-only cookies cannot be added without a URL; they must not break the batch.
    cookies = to_playwright_cookies(_jar("", ".threads.net"))
    assert [c["domain"] for c in cookies] == [".threads.net"]


def test_playwright_cookies_shape():
    cookies = to_playwright_cookies(_jar(".threads.net"))
    assert set(cookies[0]) == {"name", "value", "domain", "path", "secure"}


def test_playwright_cookies_empty_jar():
    assert to_playwright_cookies(http.cookiejar.CookieJar()) == []
    assert to_playwright_cookies(None) == []
