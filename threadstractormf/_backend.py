"""Shared helpers for talking to Meta's servers through two HTTP backends.

Two independent backends are supported:

* **httpx** — the default. No browser TLS impersonation.
* **curl_cffi** — opted into with ``--impersonate chrome``. Meta's WAF inspects
  the JA3/JA4 TLS fingerprint and the HTTP/2 SETTINGS frame, which httpx cannot
  fake; curl_cffi impersonates a real Chrome byte-for-byte, the same mechanism
  ``yt-dlp --impersonate`` uses.

Everything else (rate limiting, retries, streaming, the GraphQL client and the
media downloader) is written against the tiny surface both libraries share, so
the httpx path stays byte-identical and curl_cffi is just an opt-in branch.

Also holds the cookie converter used by the three Playwright call sites, which
used to duplicate it three times (and all three dropped the leading dot, which
made every auth cookie host-only).
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

if TYPE_CHECKING:  # pragma: no cover
    # curl_cffi is an optional extra; the alias is only needed for typing.
    from curl_cffi.requests.session import HttpMethod
else:
    HttpMethod = str

# ─── Retry classification ────────────────────────────────────────────────────
#
# Shared by the GraphQL client and the media downloader. Both exception
# families must be understood here, because curl_cffi replaces httpx wholesale
# when --impersonate is active.


def is_retryable_status(status: int) -> bool:
    """True for statuses worth retrying: 429 (rate limited) and any 5xx.

    Every other 4xx is permanent and must surface immediately. A 403 from
    Meta's WAF will not fix itself, so retrying it only burns the full
    exponential backoff (~60s) before reporting the very same error.
    """
    return status == 429 or status >= 500


def _curl_exc() -> Any:
    """curl_cffi's exception module, or None when the extra is not installed."""
    try:
        from curl_cffi.requests import exceptions as curl_exceptions
    except ImportError:
        return None
    return curl_exceptions


def is_transient_error(exc: BaseException) -> bool:
    """True when a failure is worth retrying (flaky network, DNS, CDN hiccup).

    Handles both backends. Order matters: curl_cffi's ``RequestException``
    subclasses ``OSError``, so the curl branch must be tested *before* the
    generic ``OSError`` branch or every curl error would be misclassified as a
    plain OSError with no errno and never retried.
    """
    curl = _curl_exc()
    if curl is not None:
        # Raised by Response.raise_for_status(): retry only if the status allows.
        if isinstance(exc, curl.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return status is not None and is_retryable_status(int(status))
        # ConnectTimeout, ReadTimeout, ConnectionError (incl. DNSError, SSLError).
        if isinstance(exc, (curl.Timeout, curl.ConnectionError)):
            return True
        # Any other curl RequestException (InvalidURL, TooManyRedirects, ...)
        # is a permanent programming/URL problem.
        if isinstance(exc, curl.RequestException):
            return False

    if isinstance(exc, httpx.HTTPStatusError):
        return is_retryable_status(exc.response.status_code)
    if isinstance(exc, httpx.TransportError):
        # ConnectError (wraps gaierror/[Errno -3]), ConnectTimeout, ReadTimeout,
        # RemoteProtocolError, PoolTimeout, ...
        return True
    if isinstance(exc, httpx.HTTPError):
        # InvalidURL, DecodingError, TooManyRedirects: permanent.
        return False

    if isinstance(exc, OSError):
        # EAI_AGAIN lives in `socket`, not `errno`; referencing errno.EAI_AGAIN
        # raised AttributeError on *any* OSError reaching here, including the
        # very DNS flap this branch exists to detect.
        return exc.errno in (-3, socket.EAI_AGAIN)
    return False


# ─── HTTP backend ────────────────────────────────────────────────────────────


class StreamClient(Protocol):
    """The minimum surface the media downloader needs.

    Both ``httpx.Client`` and :class:`CurlClient` satisfy it structurally,
    which is what lets ``download_media`` accept either backend. The signature
    lists only the argument the downloader actually passes, because a protocol
    declaring ``**kwargs`` would require the implementation to accept arbitrary
    keywords — which ``httpx.Client.stream`` does not.
    """

    def stream(
        self, method: str, url: str, *, headers: dict[str, str] | None = None
    ) -> Any: ...

    def close(self) -> None: ...


class _CurlStreamResponse:
    """Presents a curl_cffi streamed response through the httpx surface.

    Only one method actually differs: curl_cffi names it ``iter_content``
    while httpx calls it ``iter_bytes``. Everything else (``status_code``,
    ``raise_for_status``) already matches.
    """

    __slots__ = ("_resp",)

    def __init__(self, resp: Any) -> None:
        self._resp = resp

    @property
    def status_code(self) -> int:
        return int(self._resp.status_code)

    def raise_for_status(self) -> None:
        self._resp.raise_for_status()

    def iter_bytes(self, chunk_size: int | None = None) -> Any:
        # chunk_size is intentionally dropped: curl pulls what it wants off the
        # socket and warns loudly if we pretend otherwise.
        return self._resp.iter_content()


def known_impersonate_targets() -> set[str]:
    """Every impersonation target curl_cffi understands (generic + versioned).

    curl_cffi does **not** validate ``impersonate`` at construction time — it
    accepts any string and only misbehaves later, at request time. Validating up
    front turns a confusing runtime failure into an actionable error.
    """
    from typing import get_args

    from curl_cffi.requests.impersonate import REAL_TARGET_MAP, BrowserTypeLiteral

    return set(REAL_TARGET_MAP) | set(get_args(BrowserTypeLiteral))


class CurlClient:
    """``httpx``-shaped wrapper around a curl_cffi Session.

    Only instantiated when ``impersonate`` is requested. Three differences from
    ``httpx.Client`` are papered over here:

    * cookies are *assigned* (``session.cookies = jar``), not passed as a kwarg
    * ``timeout`` is ``(connect, total)`` — verified empirically: the second
      element is a whole-transfer budget, **not** a per-phase timeout, so it has
      to be generous enough for large media or slow CDN links get truncated
      mid-download (the same failure the httpx path fixed via a per-chunk read)
    * HTTP/2 must be requested explicitly
    """

    def __init__(
        self,
        *,
        impersonate: str,
        cookies: Any = None,
        headers: dict[str, str] | None = None,
        connect_timeout: float = 15.0,
        total_timeout: float = 600.0,
        http2: bool = True,
    ) -> None:
        try:
            from curl_cffi import requests as curl_requests
        except ImportError as exc:  # pragma: no cover - depends on env
            raise ImportError(
                "--impersonate requires curl_cffi. "
                "Install it with: pip install 'threadstractormf[antibot]'"
            ) from exc

        self.impersonate = impersonate
        if impersonate not in known_impersonate_targets():
            examples = ", ".join(sorted(known_impersonate_targets())[:6])
            raise ValueError(
                f"unknown impersonation target: {impersonate!r}. "
                f"Valid targets include: {examples} (and versioned forms like "
                "'chrome131' or 'safari18_0')"
            )

        kwargs: dict[str, Any] = {"timeout": (connect_timeout, total_timeout)}
        if http2:
            from curl_cffi.const import CurlHttpVersion

            kwargs["http_version"] = CurlHttpVersion.V2_0
        self._session = curl_requests.Session(impersonate=impersonate, **kwargs)

        # A CookieJar instance is converted by curl_cffi on assignment.
        if cookies is not None:
            self._session.cookies = cookies
        if headers:
            self._session.headers.update(headers)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        kwargs.setdefault("allow_redirects", True)
        return self._session.request(cast("HttpMethod", method), url, **kwargs)

    def stream(self, method: str, url: str, **kwargs: Any) -> Any:
        from contextlib import contextmanager

        verb = cast("HttpMethod", method)

        @contextmanager
        def _ctx() -> Any:
            with self._session.stream(verb, url, **kwargs) as resp:
                yield _CurlStreamResponse(resp)

        return _ctx()

    def close(self) -> None:
        self._session.close()


# ─── Playwright ──────────────────────────────────────────────────────────────


def to_playwright_cookies(jar: Any) -> list[dict[str, Any]]:
    """Convert a CookieJar into the dict shape ``BrowserContext.add_cookies`` wants.

    The leading dot on the domain **must be preserved**. Chromium treats a
    domain without one as host-only, so ``threads.net`` would never be sent to
    ``www.threads.net`` — i.e. every auth cookie silently dropped and the
    browser left logged out. Verified: with ``.threads.net`` the cookie arrives,
    with ``threads.net`` no ``Cookie`` header is sent at all.

    Shared by ``ThreadsAPI`` (profile pic + post scraping) and ``scripts/sniff.py``
    so the three former copies of this conversion cannot drift apart again.
    """
    out: list[dict[str, Any]] = []
    for cookie in jar or ():
        try:
            domain = getattr(cookie, "domain", "") or ""
            if not domain:
                # Host-only cookies have no domain and cannot be added without a
                # URL; skip rather than let add_cookies() fail on the whole batch.
                continue
            out.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": domain,
                    "path": getattr(cookie, "path", "/") or "/",
                    "secure": bool(getattr(cookie, "secure", True)),
                }
            )
        except Exception:
            continue
    return out

