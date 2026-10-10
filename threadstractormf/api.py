"""Threads GraphQL client — implemented via Brave sniffing of threads.com.

Sniff: POST https://www.threads.com/graphql/query
  form-encoded; the query is a *persisted* one, so the client sends no query
  text, only fb_api_req_friendly_name + the doc_id that indexes the server-side
  copy. Meta rotates that id whenever the query changes, so it is captured from
  the live web app and cached rather than hardcoded — see _discovery.py.
  variables: {"after":null,"first":11,"userID":"<userID>", ...}
  headers: X-CSRFToken, X-IG-App-ID 238260118697367, X-ASBD-ID 129477,
  Referer threads.com, lsd, jazoest
  Respuesta: data.mediaData.edges[].node.thread_items[0].post
  { id, pk, code, user, caption, taken_at, image_versions2, carousel_media, video_versions }

post_id mapping: post.code if present else raw pk. Media.id = post_id / post_id_1 for carousels.
"""

from __future__ import annotations

import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from threadstractormf._backend import (
    CurlClient,
    is_retryable_status,
    is_transient_error,
    to_playwright_cookies,
)
from threadstractormf._discovery import (
    RequestTemplate,
    build_body,
    clear_template,
    discover_and_cache,
    load_template,
    session_fingerprint,
)
from threadstractormf.auth import get_csrf_token, load_netscape_cookies
from threadstractormf.models import Media, MediaType, Post, Profile, extract_post_id
from threadstractormf.rate_limit import (
    ApiRateLimiter,
    RateLimitConfig,
    backoff_sleep,
    parse_retry_after,
)

# The GraphQL endpoint. The request's parameters — doc_id above all — are not
# hardcoded here: see threadstractormf._discovery, which captures them from the
# live web app. A hardcoded doc_id is exactly what rotted in 1.2.0.
_GRAPHQL_URL = "https://www.threads.com/graphql/query"
_X_IG_APP_ID = "238260118697367"
_X_ASBD_ID = "129477"

# Posts requested per GraphQL round trip. Kept at the value Threads was
# observed to serve; pagination (see get_posts) walks the cursor instead of
# asking for a bigger page, so a format change is less likely to break us.
_PAGE_SIZE = 12
# Hard stop for the cursor walk when no --limit is given. Sized to cover real
# profiles in full (200 pages x 12 = 2400 posts) rather than to be fast; the
# adaptive limiter in rate_limit.ApiRateLimiter handles the pacing, so a long
# walk is slow instead of risky. When a limit is set the budget is derived from
# it instead (see get_posts).
_MAX_PAGES = 200

# How many times one scrape may capture a GraphQL request template: once to get
# a starting point, once after the server rejected the one we had. Beyond that
# the problem is not a rotated id, and retrying only hammers the endpoint.
_MAX_TEMPLATE_ATTEMPTS = 2


def _warn(message: str) -> None:
    """Diagnostic on stderr.

    stdout carries the "one line per downloaded file" contract that scrapmf
    parses, so anything informational must go to stderr instead.
    """
    print(f"threadstractormf: {message}", file=sys.stderr, flush=True)


@dataclass
class ScrapeReport:
    """Counters for one get_posts() call.

    A scrape that silently returns a fraction of a profile is indistinguishable
    from a complete one unless the numbers are kept: the GraphQL path can die
    into the DOM fallback, posts can be dropped as reposts or for failing to
    parse, and the cursor walk can stop early. All of that used to be invisible
    — a 4-post scrape looked exactly like a complete one.
    """

    posts: int = 0
    media: int = 0
    unparsed: int = 0
    reposts_skipped: int = 0
    no_media: int = 0
    pages_fetched: int = 0
    truncated: bool = False
    stop_reason: str = ""
    used_fallback: bool = False
    api_error: str = ""
    limit: int | None = None
    # Filled in by the CLI as the media loop runs: download_media reports
    # whether it fetched bytes or merely recognised an existing file.
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


class _NonJsonResponse(Exception):
    """The GraphQL endpoint answered with the web app instead of JSON."""


class _StaleTemplate(Exception):
    """The captured request template no longer resolves; rediscover it.

    Distinct from every other failure because it is *recoverable*: the cached
    doc_id is simply out of date, and capturing a fresh one is enough. Raising
    here rather than silently retrying keeps the recovery in one place, where it
    can be bounded — one rediscovery per scrape, never a loop.
    """


def _finish_report(report: ScrapeReport, posts: list[Any]) -> None:
    """Fill the derived counters once the post list is final.

    Tolerates non-Post entries: tests stub the DOM fallback with sentinels, and
    a diagnostic helper must not be the thing that breaks them.
    """
    report.posts = len(posts)
    report.media = sum(len(getattr(p, "media", ())) for p in posts)
    report.no_media = sum(1 for p in posts if not getattr(p, "media", ()))


# Pacing for the DOM fallback's scroll walk. See the loop in
# _fetch_posts_via_playwright for why the wait is not jittered throughout:
# too short a wait makes the walk mistake "not rendered yet" for "end of
# profile". v1.0.1 read whole profiles with 3000ms; 91a3a45 broke that.
_SCROLL_WAIT_MS = 3000
_SCROLL_JITTER_SCROLLS = 3

# Inject the session cookies into the DOM fallback?
#
# Measured, not assumed: on a 9-post profile the fallback returns 4 posts with
# the session cookies attached and all 9 without them. Threads serves a logged-in
# client a truncated profile view whose own tab query answers
# has_next_page=false after 4 posts; the anonymous view carries the whole
# timeline. v1.0.5 scraped the profile without them (it stripped the leading
# dot off every cookie domain, which made them host-only and unsent) and read
# the account in full; v1.1.0 "fixed" the dot, the session arrived, and the
# scrape silently lost 5 of 9 posts.
#
# So the dot is correct for the download/GraphQL paths — it is what makes
# threads.net session cookies reach www.threads.net — but wrong for reading a
# public profile page. Keeping both behaviours behind one flag is why this is a
# parameter and not a revert.
_PROFILE_FALLBACK_AUTHENTICATED = False


def _require_playwright() -> None:
    """Raise an actionable ImportError when the browser extra is missing.

    Without this, a fallback to the DOM scraper surfaces as a bare
    ``ImportError: No module named 'playwright'``, which says nothing about how
    to fix it. ``playwright`` lives in the optional ``browser`` extra, and the
    browser binaries are a separate download, so both steps are spelled out.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "playwright is required for this step but is not installed.\n"
            "  pip install 'threadstractormf[browser]'\n"
            "  playwright install chromium\n"
            "It is only needed when the GraphQL API path is unavailable; if you "
            "only need the fast path, check that your cookies are valid."
        ) from exc


def _guess_ext(url: str) -> str:
    """Best-effort file extension from the URL *path*.

    Extension is read from the path segment itself rather than by searching the
    whole URL: an image like ".../video_cover.jpg" used to be tagged `.mp4`
    because the path contained the word "video", which produced files whose
    extension disagreed with their contents.
    """
    path = urlparse(url).path.lower()
    last_segment = path.rsplit("/", 1)[-1]
    suffix = last_segment.rsplit(".", 1)[-1] if "." in last_segment else ""
    # "jpg" is kept as-is (not folded into "jpeg") so filenames stay identical
    # to previous versions and existing archives still dedup by name.
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "avif", "heic", "bmp"}:
        return suffix
    video = {"mp4", "webm", "mov", "m4v"}
    if suffix in video:
        return suffix
    # No usable extension in the path: fall back to the URL as a whole, which
    # is how Threads serves some CDN URLs.
    low = url.lower()
    if ".mp4" in low or ".webm" in low or ".mov" in low:
        return "mp4" if ".mp4" in low else ("webm" if ".webm" in low else "mov")
    return "jpg"


def _pick_best_image(candidates: list[dict]) -> str | None:
    if not candidates:
        return None
    # mayor width primero
    try:
        c = max(candidates, key=lambda x: int(x.get("width", 0)))
        return c.get("url")
    except Exception:
        return candidates[0].get("url")


def _pick_best_video(versions: Any) -> str | None:
    """Best MP4 URL out of a ``video_versions`` list.

    Instagram orders these ascending by bitrate, so index 0 is the *worst*
    rendition — picking it silently threw away resolution. Prefer the entry
    with the largest pixel area, falling back to pixel count and then to the
    last element (the highest rung in the ladder).
    """
    if not isinstance(versions, list) or not versions:
        return None
    candidates = [v for v in versions if isinstance(v, dict) and v.get("url")]
    if not candidates:
        return None

    def score(item: dict) -> tuple[int, int]:
        return (int(item.get("width") or 0) * int(item.get("height") or 0),
                int(item.get("width") or 0))

    best = max(candidates, key=score)
    if score(best)[0] == 0 and len(candidates) > 1:
        # No dimensions reported: trust the ordering and take the top rung.
        best = candidates[-1]
    url = best.get("url")
    return str(url) if url else None


def _parse_profile_html(html: str) -> tuple[str | None, str | None, str | None]:
    """Extract ``(user_id, lsd, profile_pic_url)`` from a profile page.

    A single pass over the document, shared by ``_resolve_user_id`` and
    ``get_profile`` so one page fetch serves both.

    Only ``userID`` identifies the account. The previous fallback to ``pk``
    matched the *first* ``pk`` in the document, which belongs to a post — the
    same field ``_parse_post_node`` treats as a post id — and then used it as
    the GraphQL ``userID``. That produced empty results and a silent, very slow
    fallback to the DOM scraper, with no way to tell the user why.
    """
    user_id = None
    lsd = None
    pic = None

    m = re.search(r'"userID"\s*:\s*"(\d+)"', html)
    if m:
        user_id = m.group(1)

    # Threads stopped shipping ``"lsd":"<token>"`` and now nests it in the
    # Relay require payload as ``["LSD",[],{"token":"<token>"},<prio>]``. The
    # old regex silently matched nothing, so every request went out with an
    # empty X-FB-LSD and the query was rejected. Both shapes are accepted
    # because which one ships depends on the rollout, not on our side.
    m = re.search(r'\["LSD",\s*\[\],\s*\{"token":"([^"]+)"', html)
    if m:
        lsd = m.group(1)
    else:
        m = re.search(r'"lsd"\s*:\s*"([^"]+)"', html)
        if m:
            lsd = m.group(1)

    m = re.search(r'"profile_pic_url"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if m:
        # The value is JSON-escaped inside the HTML blob. Only ``\u0026`` and
        # ``\/`` matter for a URL, and leaving ``\/`` in place produces a URL the
        # CDN will not serve.
        pic = m.group(1).replace(r"\u0026", "&").replace(r"\/", "/")

    return user_id, lsd, pic


def _parse_post_node(node: dict, fallback_username: str | None = None) -> Post | None:
    # node = {"thread_items": [{"post": {...}}]}
    try:
        items = node.get("thread_items") or []
        if not items:
            return None
        post = items[0].get("post")
        if not post:
            return None
        # reposts: if post.user.username != fallback_username and excluded, filtered above
        pk = str(post.get("pk") or "")
        code = post.get("code")
        # code is the short post_id like DKxyz (threads.net/@user/post/<code>)
        post_id = code if code else pk
        if not post_id:
            return None
        username = post.get("user", {}).get("username") or fallback_username or "unknown"
        permalink = f"https://www.threads.com/@{username}/post/{post_id}"
        # caption
        cap = post.get("caption", {})
        caption = None
        if isinstance(cap, dict):
            caption = cap.get("text")
        # datetime from taken_at (unix)
        taken = post.get("taken_at")
        datetime_iso = None
        if taken:
            try:
                from datetime import datetime, timezone

                datetime_iso = datetime.fromtimestamp(int(taken), tz=timezone.utc).isoformat()
            except Exception:
                pass
        # counts (no vienen en mediaData, set 0)
        like_count = 0
        # carousel
        medias: list[Media] = []
        vtype: MediaType = "image"
        carousel = post.get("carousel_media")
        # single media case
        if carousel and isinstance(carousel, list) and len(carousel) > 0:
            for idx, cm in enumerate(carousel):
                # cm has image_versions2 or video_versions
                url = None
                vtype = "image"
                if cm.get("video_versions"):
                    url = _pick_best_video(cm["video_versions"])
                    vtype = "video"
                if not url:
                    iv2 = cm.get("image_versions2", {})
                    url = _pick_best_image(iv2.get("candidates", []))
                if not url:
                    continue
                ext = _guess_ext(url)
                mid = post_id if len(carousel) == 1 else f"{post_id}_{idx+1}"
                medias.append(
                    Media(id=mid, post_id=post_id, index=idx + 1, type=vtype, url=url, ext=ext)
                )
        else:
            # single image/video
            url = None
            vtype = "image"
            if post.get("video_versions"):
                url = _pick_best_video(post["video_versions"])
                vtype = "video"
            if not url:
                iv2 = post.get("image_versions2", {})
                url = _pick_best_image(iv2.get("candidates", []))
            if url:
                ext = _guess_ext(url)
                medias.append(
                    Media(id=post_id, post_id=post_id, index=1, type=vtype, url=url, ext=ext)
                )
            else:
                # text only? skip if no media (we do not want text-only posts)
                pass
        # no medias: skip entirely (user asked for photos/videos only)
        if not medias:
            return None
        return Post(
            id=post_id,
            permalink=permalink,
            username=username,
            caption=caption,
            datetime_iso=datetime_iso,
            like_count=like_count,
            media=medias,
        )
    except Exception:
        return None


def _walk_xhr_videos(node: Any, found: dict[str, list[str]]) -> None:
    """Recursively collect {post_code: [mp4 urls]} from threads site API JSON.

    Post objects carry "code" plus video_versions at their own level or inside
    carousel_media items; this walks arbitrary response trees.
    """
    if isinstance(node, dict):
        code = node.get("code")
        if isinstance(code, str):
            urls: list[str] = []
            vv = node.get("video_versions")
            if isinstance(vv, list):
                for v in vv:
                    if isinstance(v, dict) and ".mp4" in (v.get("url") or ""):
                        urls.append(str(v["url"]))
            car = node.get("carousel_media")
            if isinstance(car, list):
                for item in car:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("video_versions"), list)
                    ):
                        for v in item["video_versions"]:
                            if isinstance(v, dict) and ".mp4" in (v.get("url") or ""):
                                urls.append(str(v["url"]))
            if urls:
                found[code].extend(urls)
        for v in node.values():
            _walk_xhr_videos(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk_xhr_videos(v, found)


class ThreadsAPI:
    """Low-level client. Used by Threadscraper."""

    def __init__(
        self,
        cookies: httpx.Cookies | Any | str | Path,
        *,
        impersonate: str | None = None,
        timeout: float = 15.0,
        rate_limit_config: RateLimitConfig | None = None,
    ):

        if isinstance(cookies, (str, Path)):
            cookies = load_netscape_cookies(cookies)
        self.cookies = cookies
        self.impersonate = impersonate
        self.timeout = timeout
        self._client: Any = None
        # True only for a client this class built. An injected one (tests, a
        # caller supplying their own session) is never discarded or rebuilt.
        self._owns_client = False
        self.rate_limit_config = rate_limit_config or RateLimitConfig()
        self._api_limiter = ApiRateLimiter(
            rps=self.rate_limit_config.rps, enabled=self.rate_limit_config.enabled
        )
        self._user_id_cache: dict[str, str] = {}
        # Profile HTML is fetched by both user-id resolution and get_profile; a
        # scrape that does both should not pay for two requests.
        self._profile_cache: dict[str, str] = {}
        self._lsd: str | None = None
        # Replayable GraphQL request parameters, discovered from the web app.
        # Keyed by username: the captured variables carry the userID of the
        # account the browser queried, which is only valid for that profile.
        self._templates: dict[str, RequestTemplate] = {}
        self._template: RequestTemplate | None = None
        self._template_attempted: set[str] = set()
        self._template_attempts = 0
        self._fingerprint_cache: str | None = None
        self._has_session_cache: bool | None = None
        # Counters for the most recent get_posts() call, so the CLI can report a
        # truncated scrape instead of ending in silence.
        self.last_report = ScrapeReport()

    def _headers(self) -> dict[str, str]:
        csrf = get_csrf_token(self.cookies) or ""
        # The captured template carries the lsd the request was built with, and
        # it must agree with the body's lsd: Threads pairs the two. Prefer the
        # template's; fall back to the one parsed off the profile page.
        lsd = ""
        if self._template is not None:
            lsd = self._template.params.get("lsd", "")
        if not lsd:
            lsd = self._lsd or ""
        h = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "X-CSRFToken": csrf,
            "X-IG-App-ID": _X_IG_APP_ID,
            "X-ASBD-ID": _X_ASBD_ID,
            "Referer": "https://www.threads.com/",
            "X-FB-LSD": lsd,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
        }
        return h

    def _get_client(self) -> Any:
        """Return the shared HTTP client.

        ``curl_cffi`` (Chrome TLS/JA4 impersonation) when ``impersonate`` was
        requested, otherwise ``httpx`` — the default path stays untouched.
        """
        if self._client:
            return self._client
        self._owns_client = True
        if self.impersonate:
            # curl_cffi takes (connect, total): the read budget must cover a
            # whole GraphQL response, so scale it off the public `timeout`.
            self._client = CurlClient(
                impersonate=self.impersonate,
                cookies=self.cookies,
                headers=self._headers(),
                connect_timeout=5.0,
                total_timeout=max(30.0, self.timeout * 2),
            )
            return self._client
        # fine-grained: connect fast-fails at 5s; read uses the public
        # `timeout` param (per-chunk, so slow-but-active responses survive)
        site_timeout = httpx.Timeout(
            connect=5.0, read=self.timeout, write=10.0, pool=5.0
        )
        self._client = httpx.Client(
            cookies=self.cookies,
            headers=self._headers(),
            timeout=site_timeout,
            follow_redirects=True,
            http2=True,
        )
        return self._client

    def _request_with_rate_limit(self, method: str, url: str, **kwargs) -> Any:
        """Rate-limited request with retries for *transient* failures only.

        Retries are driven by ``is_transient_error`` / ``is_retryable_status``,
        which cover both the httpx and the curl_cffi exception families. In
        particular a permanent 4xx (403 from Meta's WAF, 404, 400) now surfaces
        on the first attempt instead of burning the full exponential backoff
        (~60s) before reporting the very same error.

        Honours ``Retry-After`` when the server sends it, otherwise falls back to
        exponential backoff.
        """
        client = self._get_client()
        if self.rate_limit_config.enabled:
            self._api_limiter.wait()

        max_retries = self.rate_limit_config.max_retries
        base = self.rate_limit_config.backoff_base
        last_exc: BaseException | None = None

        for attempt in range(max_retries + 1):
            try:
                resp = client.request(method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - re-classified below
                # Transport-level failure (DNS, timeout, TLS, reset...).
                if attempt < max_retries and is_transient_error(exc):
                    last_exc = exc
                    backoff_sleep(attempt, base=base)
                    continue
                raise

            # Retryable status (429/5xx): honour Retry-After, else back off.
            if is_retryable_status(resp.status_code):
                if resp.status_code == 429:
                    # Tell the limiter the server objected, so every later
                    # request spaces out even after this one succeeds.
                    self._api_limiter.penalize()
                if attempt < max_retries:
                    retry_after = parse_retry_after(dict(resp.headers))
                    if retry_after is not None:
                        time.sleep(retry_after)
                    else:
                        backoff_sleep(attempt, base=base)
                    continue
                last_exc = None
                resp.raise_for_status()  # out of retries: surface the failure
                return resp  # pragma: no cover - raise_for_status always raises

            # 2xx/3xx, or a permanent 4xx: raise_for_status() raises right away
            # for the latter, which is exactly what we want.
            resp.raise_for_status()
            self._api_limiter.relax()
            return resp

        # The loop only falls through when every attempt hit a retryable status
        # and max_retries is exhausted on the last one (handled above), so this
        # is only reachable if max_retries is negative.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("request failed without an exception")

    def _profile_html(self, username: str) -> str | None:
        """Fetch and cache the profile page for ``username``.

        Shared by user-id resolution and the profile lookup so that a scrape
        which does both (the CLI does) pays for a single request instead of two.
        """
        cached = self._profile_cache.get(username)
        if cached is not None:
            return cached
        try:
            resp = self._request_with_rate_limit(
                "GET", f"https://www.threads.com/@{username}"
            )
        except Exception:
            return None
        html = str(resp.text)
        self._profile_cache[username] = html
        return html

    def _has_session(self) -> bool:
        """Whether these cookies identify a logged-in account.

        Gates browser-based discovery, which can only capture what a
        logged-in page actually issues.
        """
        if self._has_session_cache is None:
            self._has_session_cache = session_fingerprint(self.cookies) != "anonymous"
        return self._has_session_cache

    def _fingerprint(self) -> str:
        """Cache key for the current session's captured template.

        Computed once: the cookies do not change over a client's life, and this
        runs on every page of the walk.
        """
        if self._fingerprint_cache is None:
            self._fingerprint_cache = session_fingerprint(self.cookies)
        return self._fingerprint_cache

    def _ensure_template(self, username: str) -> RequestTemplate | None:
        """Return a usable request template, discovering one if needed.

        Order: in-memory, then disk cache, then a browser capture.

        Two separate bounds, deliberately different in scope:

        * the per-profile guard stops the *same* target being re-attempted
          within one scrape, so "the browser is missing" fails once and fast
          instead of stalling on every page;
        * :attr:`_template_attempts` is global, because a batch scrape across
          many profiles must still not spend unbounded time in the browser.
        """
        key = username.lstrip("@").split("/")[0]
        cached_in_memory = self._templates.get(key)
        if cached_in_memory is not None:
            self._template = cached_in_memory
            return cached_in_memory

        # The cache is per (session, account): the captured tokens are bound to
        # the session that produced them, and the captured variables name the
        # profile the browser visited.
        fingerprint = self._fingerprint()
        cached = load_template(fingerprint, key)
        if cached is not None:
            self._templates[key] = cached
            self._template = cached
            return cached

        # Per profile, not per instance. A scraper reused across several
        # accounts must be able to discover for each one: a capture that
        # failed (or was skipped) for @a says nothing about @b. As a plain
        # flag this locked discovery out after the first target, so every
        # subsequent profile silently fell back to the DOM path.
        if key in self._template_attempted:
            return None
        self._template_attempted.add(key)

        # The two checks below abort without attempting anything, so they must
        # come *before* the budget is spent. Counting them turned a batch of
        # anonymous profiles into "the query was refused again after
        # recapturing its id" — which never happened, there was simply no
        # session to capture from and no browser to capture with.

        # Only a logged-in browser issues the media query. Verified: an
        # anonymous profile page fires only fetchBarcelonaExperimentQuery, so
        # launching a browser to capture the media query provably finds
        # nothing — about 15 wasted seconds on every anonymous scrape, for a
        # path that then falls back to the DOM scraper anyway. The captured
        # lsd and fb_dtsg are session-bound too, so a template captured for
        # one account cannot be replayed by another (measured: execution
        # error). Anonymous scraping keeps using the DOM path.
        if not self._has_session():
            _warn(
                "no GraphQL template cached and no session cookies, so the query "
                "cannot be discovered (Threads only issues it to a logged-in "
                "browser); using the DOM scraper, which is slower but works "
                "anonymously. Pass --cookies or --cookies-from-browser for the "
                "fast API path."
            )
            return None

        try:
            _require_playwright()
        except ImportError:
            # Degrade cleanly: no template, no browser, so the fast path is
            # impossible for this run. Say exactly how to enable it rather than
            # failing with a bare ImportError later.
            _warn(
                "no cached GraphQL template and the browser extra is missing, so "
                "the query parameters cannot be discovered. Install "
                "'threadstractormf[browser]' and 'playwright install chromium' to "
                "enable the fast API path."
            )
            return None

        # Everything above only checks whether an attempt is possible; from
        # here the browser really launches, so this is where the budget is
        # spent. Global on purpose: a batch scrape must not spend unbounded
        # time in the browser, however many profiles it visits.
        self._template_attempts += 1
        if self._template_attempts > _MAX_TEMPLATE_ATTEMPTS:
            # Two captures, two rejections: something structural changed.
            # Stop here rather than looping against Threads.
            _warn(
                "the GraphQL query was refused again after recapturing its id; "
                "stopping the fast path for this run"
            )
            return None

        try:
            template = discover_and_cache(username, self.cookies)
        except Exception as exc:
            _warn(
                f"could not capture the GraphQL request template "
                f"({exc.__class__.__name__}); falling back to the DOM scraper"
            )
            return None

        self._templates[username.lstrip("@").split("/")[0]] = template
        self._template = template
        _warn(f"captured a current GraphQL doc_id ({template.doc_id})")
        return template

    def _invalidate_template(self) -> None:
        """Drop the template the server just rejected.

        Both the in-memory copy and the disk cache go, so the next attempt
        rediscovers rather than replaying a doc_id known to be dead.

        The per-profile guard is cleared as well: this is the server telling us
        the id is wrong, which is a *different* situation from having no way to
        get one, and it deserves its own capture. :attr:`_template_attempts` is
        what stops that from looping, and it stays global on purpose — a scrape
        must not spend unbounded time in the browser across many profiles.
        """
        username = self._template.username if self._template else ""
        self._templates.pop(username, None)
        self._template = None
        self._template_attempted.discard(username)
        clear_template(self._fingerprint(), username)

    def _resolve_user_id(self, username: str) -> str:
        if username in self._user_id_cache:
            return self._user_id_cache[username]

        html = self._profile_html(username)
        if html is not None:
            user_id, lsd, _pic = _parse_profile_html(html)
            if user_id:
                self._user_id_cache[username] = user_id
                if lsd:
                    self._lsd = lsd
                return user_id
            # No userID: fall through to the DOM scraper rather than burning a
            # GraphQL call on an id we already know is unreliable.
        raise ValueError(f"could not resolve userID for @{username}")

    def get_profile(self, username: str) -> Profile:
        username = username.lstrip("@").split("/")[0]
        try:
            html = self._profile_html(username)
            user_id = lsd = pic = None
            if html is not None:
                user_id, lsd, pic = _parse_profile_html(html)
                if lsd:
                    self._lsd = lsd
                # Seed the cache so get_posts() can skip its own page fetch.
                if user_id:
                    self._user_id_cache[username] = user_id

            if not pic:
                # Best-effort: without a pic in the HTML, ask the browser.
                try:
                    pic = self._fetch_profile_pic_via_playwright(username)
                except Exception as exc:
                    _warn(
                        f"could not read the avatar for @{username} "
                        f"({exc.__class__.__name__}); continuing without it"
                    )

            profile_pic_media = None
            if pic:
                from threadstractormf.models import sanitize_filename

                stem = f"{sanitize_filename(username)}_profile"
                profile_pic_media = Media(
                    id=stem,
                    post_id=stem,
                    index=1,
                    type="image",
                    url=pic,
                    ext=_guess_ext(pic),
                )
            return Profile(
                username=username,
                user_id=user_id,
                profile_pic_url=pic,
                profile_pic_media=profile_pic_media,
            )
        except Exception as e:
            raise RuntimeError(f"get_profile failed for @{username}: {e}") from e

    def _fetch_profile_pic_via_playwright(self, username: str) -> str | None:
        try:
            # Optional enrichment: a missing browser here is not fatal, the caller
            # just gets no avatar. Warn instead of failing silently.
            _require_playwright()
            from playwright.sync_api import sync_playwright

            # NB: the leading dot on the cookie domain must survive — Chromium
            # would otherwise treat every cookie as host-only and send nothing
            # to www.threads.com, leaving the browser logged out.
            pw = to_playwright_cookies(self.cookies)
            with sync_playwright() as p:
                b = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx = b.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    )
                )
                if pw:
                    ctx.add_cookies(cast("Any", pw))
                page = ctx.new_page()
                page.goto(f"https://www.threads.com/@{username}", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                pic = page.evaluate(
                    """() => document.querySelector('img[src*="t51.2885-19"]')?.src
                        || document.querySelector('img[src*="s150x150"][src*="profile"]')?.src
                        || null"""
                )
                b.close()
                return str(pic) if pic else None
        except Exception:
            return None

    def _fetch_posts_via_playwright(
        self,
        username: str,
        limit: int | None,
        *,
        authenticated: bool = _PROFILE_FALLBACK_AUTHENTICATED,
    ) -> list[Post]:
        from collections import defaultdict

        _require_playwright()
        from playwright.sync_api import sync_playwright

        # See _PROFILE_FALLBACK_AUTHENTICATED: an anonymous context reads the
        # whole profile, a logged-in one is served a truncated view.
        pw = to_playwright_cookies(self.cookies) if authenticated else []
        posts: list[Post] = []
        # XHR capture: threads ships media JSON (incl video_versions) via its own
        # api responses while scrolling; late posts are ONLY available here,
        # never serialized into document HTML.
        captured: list[Any] = []

        def _on_response(resp: Any) -> None:
            try:
                ct = resp.headers.get("content-type", "")
                if "json" not in ct:
                    return
                u = resp.url.lower()
                if "graphql" not in u and "/api/" not in u:
                    return
                captured.append(resp.json())
            except Exception:
                pass

        with sync_playwright() as p:
            b = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
            ctx = b.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            )
            if pw:
                try:
                    ctx.add_cookies(cast("Any", pw))
                except Exception:
                    pass
            page = ctx.new_page()
            page.on("response", _on_response)
            page.goto(f"https://www.threads.com/@{username}/media", wait_until="domcontentloaded")
            # human-like jitter: fixed intervals look robotic to bot detection
            initial_wait_ms = random.uniform(2800, 3600)
            page.wait_for_timeout(initial_wait_ms)
            # scroll patiently: stop only after several consecutive empty rounds
            # (threads lazy-loads with delay; waits carry jitter for stealth).
            # Two empty rounds at ~1.8s was not enough patience: the page had
            # simply not finished fetching, so the walk declared the profile
            # finished after the first viewport and returned 4 posts out of a
            # much longer timeline without any warning.
            max_scrolls = 60
            empty_rounds_limit = 5
            seen_permalinks: set[str] = set()
            empty_rounds = 0
            prev_time_count = 0
            data: list[dict[str, Any]] = []
            for scroll in range(max_scrolls):
                page.mouse.wheel(0, 4000)
                # Per-scroll pacing is what decides whether the profile is read
                # in full. v1.0.1 waited 3000ms per scroll and needed 3 empty
                # rounds to stop; 91a3a45 ("jittered waits") cut the wait to
                # 1350-2250ms and the threshold to 2, and the scrape has
                # returned only the first viewport ever since — 4 of 9 posts,
                # silently. Threads needs well over 1.8s to render the next
                # batch, so a shorter wait reads "nothing new" as "no more
                # posts" and stops early.
                #
                # The jitter is kept, but only where it was never the problem:
                # the first scrolls, which is what a bot detector actually
                # samples. Deep into the walk, coverage wins.
                if scroll < _SCROLL_JITTER_SCROLLS:
                    scroll_wait_ms = random.uniform(_SCROLL_WAIT_MS, _SCROLL_WAIT_MS * 1.6)
                else:
                    scroll_wait_ms = _SCROLL_WAIT_MS
                page.wait_for_timeout(scroll_wait_ms)
                time_count = page.evaluate(
                    "() => document.querySelectorAll('time[datetime]').length"
                )
                if time_count == prev_time_count:
                    empty_rounds += 1
                    if empty_rounds >= empty_rounds_limit:
                        break
                    # skip the heavy extraction when nothing new rendered
                    continue
                else:
                    empty_rounds = 0
                prev_time_count = time_count
                # check if new posts loaded
                data = page.evaluate(r"""() => {
                    const html = document.documentElement.innerHTML;
                    // valid codes: only those appearing in real /post/ links
                    // (filters false positives like "code":"en_US")
                    const validCodes = new Set();
                    document.querySelectorAll('a[href*="/post/"]').forEach(a=>{
                        const m=a.href.match(/\/post\/([^\/?#]+)/);
                        if(m) validCodes.add(m[1]);
                    });
                    // positions of every valid code occurrence in full html
                    const codePositions=[];
                    for(const m of html.matchAll(/"code":"([^"]+)"/g)){
                        if(validCodes.has(m[1])) codePositions.push({code:m[1], idx:m.index});
                    }
                    codePositions.sort((a,b)=>a.idx-b.idx);
                    // GLOBAL video map: every non-empty video_versions array is
                    // assigned to the nearest valid code AFTER it in the html.
                    // (empirically each post's media JSON follows its code; a fixed
                    // window around the first occurrence misses carousels/videos)
                    const codeVideos={};
                    const seenBase=new Set();
                    for(const m of html.matchAll(/"video_versions":\[([^\]]*)\]/g)){
                        if(!m[1].trim()) continue;
                        const um=m[1].match(/"url":"([^"]+)"/);
                        if(!um) continue;
                        const u=um[1].replaceAll(/\\u0026/g,"&")
                            .replaceAll(/\\u0025/g,"%").replace(/\\\//g,"/");
                        if(!u.includes('.mp4')) continue;
                        const base=u.split('?')[0];
                        if(seenBase.has(base)) continue;
                        seenBase.add(base);
                        let owner=null;
                        for(const cp of codePositions){
                            if(cp.idx>=m.index && cp.idx-m.index<=80000){ owner=cp.code; break; }
                        }
                        if(!owner && codePositions.length){
                            owner=codePositions[codePositions.length-1].code;
                        }
                        if(owner){
                            (codeVideos[owner]=codeVideos[owner]||[]).push(u);
                        }
                    }
                    const posts=[];
                    document.querySelectorAll('time[datetime]').forEach(t=>{
                        const a=t.closest('a[href*="/post/"]');
                        const permalink=a?a.href:null;
                        if(!permalink) return;
                        const dt=t.getAttribute('datetime');
                        let container=t;
                        let media_urls=[];
                        let postCode = (permalink.match(/\/post\/([^/?#]+)/) || [])[1] || "";
                        // poster extraction per container (images + visible posters)
                        for(let i=0;i<10 && media_urls.length==0;i++){
                            let els=[...container.querySelectorAll('img, video, source')];
                            media_urls=els.filter(el=>{
                                const s=el.src||el.srcset||"";
                                if(!s) return false;
                                if(s.includes('t51.2885-19')) return false;
                                // Host suffix, not substring: a substring test
                                // accepts any host embedding the name
                                // (instagram.evil.test). The bare ".mp4"
                                // alternative used to be here and bypassed the
                                // host check entirely.
                                try{
                                    const host=new URL(s, location.href).hostname;
                                    return [".cdninstagram.com",".cdninstagram.net",".fbcdn.net"]
                                        .some(d=>host===d.slice(1)||host.endsWith(d));
                                }catch(e){ return false; }
                            }).map(el=>el.src||el.srcset.split(' ')[0]);
                            if(media_urls.length>0) break;
                            container=container.parentElement;
                            if(!container) break;
                        }
                        // merge globally-mapped video urls for this post
                        if(postCode && codeVideos[postCode]){
                            for(const u of codeVideos[postCode]) media_urls.push(u);
                        }
                        posts.push({permalink, datetime:dt, media_urls: [...new Set(media_urls)]});
                    });
                    return posts;
                }""")
                # Strict filter: only posts whose permalink contains /@<target>/ (exact user)
                target_lower = username.lower()
                data = [d for d in data if f"/@{target_lower}/" in d["permalink"].lower()]
                # NOTE: videos are covered by the in-page global video map and the
                # XHR response capture; per-post detail fetches were removed —
                # post pages no longer serialize video_versions (Meta format change).
                # deduplicate
                new_posts = []
                for d in data:
                    if d["permalink"] not in seen_permalinks:
                        seen_permalinks.add(d["permalink"])
                        new_posts.append(d)
                # convert to Post
                for d in new_posts:
                    pid = extract_post_id(d["permalink"])
                    if not pid:
                        continue
                    # simple video detection; assume image otherwise
                    medias = []
                    for idx, u in enumerate(d["media_urls"]):
                        from threadstractormf.downloader import is_valid_media_url

                        if not is_valid_media_url(u):
                            continue
                        ext = _guess_ext(u)
                        mid = pid if len(d["media_urls"]) == 1 else f"{pid}_{idx+1}"
                        medias.append(
                            Media(
                                id=mid, post_id=pid, index=idx + 1,
                                type="video" if ext == "mp4" else "image",
                                url=u, ext=ext,
                            )
                        )
                    if not medias:
                        continue
                    posts.append(
                        Post(
                            id=pid,
                            permalink=d["permalink"],
                            username=username,
                            datetime_iso=d["datetime"],
                            media=medias,
                        )
                    )
                    if limit is not None and len(posts) >= limit:
                        break
                if limit is not None and len(posts) >= limit:
                    break
                # NOTE: no early break on empty rounds here — the scroll loop
                # already stops after 3 consecutive rounds without new renders
            b.close()
        # merge XHR-captured videos into converted posts (late posts only exist
        # here — their media JSON is never serialized into document HTML)
        xhr_map: dict[str, list[str]] = defaultdict(list)
        for j in captured:
            _walk_xhr_videos(j, xhr_map)
        for post in posts:
            extra = xhr_map.get(post.id, [])
            if not extra:
                continue
            existing = {m.url.split("?")[0] for m in post.media}
            for u in extra:
                base = u.split("?")[0]
                if base in existing:
                    continue
                existing.add(base)
                ext = _guess_ext(u)
                idx_next = len(post.media) + 1
                post.media.append(
                    Media(
                        id=f"{post.id}_{idx_next}",
                        post_id=post.id,
                        index=idx_next,
                        type="video" if ext == "mp4" else "image",
                        url=u,
                        ext=ext,
                    )
                )
        if limit is not None:
            posts = posts[:limit]
        # repost filter already implicit: username == target (DOM filtered)
        return posts

    def _walk_graphql(
        self,
        username: str,
        user_id: str | None,
        *,
        limit: int | None,
        exclude_reposts: bool,
        report: ScrapeReport,
        collected: list[Post],
    ) -> list[Post]:
        """Walk the profile cursor, appending into ``collected``.

        ``collected`` is passed in rather than returned so that posts parsed
        before a mid-walk failure survive the exception: a crawl that dies on
        page 7 still has six pages of good output, and dropping them would
        throw away work that was already paid for.

        Split out of :meth:`get_posts` so the stale-template retry can re-enter
        it from scratch with a freshly captured template, instead of resuming a
        walk whose cursor belongs to parameters that no longer resolve.

        ``userID`` is read from the captured template rather than from the
        profile HTML: the HTML carries several ``userID`` fields and the first
        one is the *viewer's own* account, not the profile being read — using it
        made every GraphQL query ask for the wrong account, which Threads
        refuses with ``execution error``. The template's variables name the
        account the browser actually queried, which is by construction the
        right one.
        """
        template = self._ensure_template(username)
        if template is None:
            # No template and no way to get one: the walk cannot start. Report
            # the real reason (already warned by _ensure_template) rather than
            # sending a request known to fail, and let the caller fall back.
            if not report.api_error:
                report.api_error = (
                    "no GraphQL request template available and discovery is "
                    "unavailable"
                )
            raise _NonJsonResponse(report.api_error)

        # The client bakes X-FB-LSD in at construction, and the lsd it needs
        # comes from this template — discovered *after* the first request of the
        # process already built the client (the profile fetch runs first).
        # Rebuilding is how the new lsd is picked up. Patching the header dict
        # in place only worked for httpx, and CurlClient (--impersonate) has no
        # ``headers`` at all, which raised AttributeError on the path the README
        # recommends for the WAF.
        #
        # Once per walk, not per page: this used to sit inside the loop below,
        # so a 200-page crawl paid 200 TLS handshakes for no reason.
        #
        # Only a client this class built is replaced. An injected one (tests, a
        # caller supplying their own session) is left alone: discarding it would
        # silently swap a mocked transport for a real one and send the request
        # to the network.
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

        target_id = template.variables.get("userID") or template.user_id or user_id
        if not target_id:
            raise _NonJsonResponse("could not determine the target account id")

        variables = {
            "after": None,
            "allow_page_info_for_lox_user": False,
            "before": None,
            "first": _PAGE_SIZE,
            "last": None,
            "userID": target_id,
            "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
        }
        out: list[Post] = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        # With a limit, never fetch more pages than could be needed.
        # Reposts get filtered and posts are deduped, so a page can
        # yield fewer than _PAGE_SIZE usable posts: leave headroom.
        # _MAX_PAGES stays a hard ceiling so an absurd --limit cannot
        # turn into an unbounded walk.
        page_budget = _MAX_PAGES
        if limit is not None:
            page_budget = min(_MAX_PAGES, max(1, -(-limit // _PAGE_SIZE) + 2))
        for _ in range(page_budget):
            variables["after"] = cursor
            body = build_body(template, variables)
            resp = self._request_with_rate_limit("POST", template.endpoint, data=body)
            report.pages_fetched += 1
            # Threads answers 200 text/html (the web app shell) when the
            # query is rejected — stale doc_id, a rotated __rev, a
            # missing LSD. That used to raise inside resp.json(), get
            # swallowed by the broad except below and surface only as
            # "falling back to the DOM scraper", hiding the real cause.
            # Decode defensively rather than trusting the content-type:
            # a real GraphQL reply may omit the header, and what matters
            # is whether the body parses as JSON at all.
            try:
                j = resp.json()
            except ValueError as exc:
                ctype = (resp.headers.get("content-type") or "no content-type")
                report.api_error = (
                    f"GraphQL returned {resp.status_code} {ctype.split(';')[0]} "
                    f"instead of JSON ({exc.__class__.__name__}); "
                    "stale doc_id or __rev"
                )
                raise _NonJsonResponse(report.api_error) from exc
            if not (j.get("data") and j["data"].get("mediaData")):
                # Distinguish "the server refused the query" from "the
                # payload was merely partial". Threads answers a stale
                # doc_id / rotated __rev with 200 JSON carrying
                # errors: [{"severity": "CRITICAL", ...}] and no data —
                # indistinguishable from an empty profile unless the
                # errors field is inspected, which it never was.
                errors = j.get("errors") or []
                if errors:
                    msgs = "; ".join(
                        str(e.get("message", e))[:60]
                        for e in errors[:2]
                        if isinstance(e, dict)
                    ) or "no detail"
                    report.api_error = f"GraphQL returned errors: {msgs}"
                    report.stop_reason = "the API rejected the query"
                    # The cached template just proved it no longer
                    # resolves. Throw it away and capture a fresh one
                    # from the web app, then let the walk be retried by
                    # the caller rather than dying on a stale id.
                    self._invalidate_template()
                    raise _StaleTemplate(report.api_error)
                else:
                    report.stop_reason = "mediaData absent"
                break  # partial/absent payload: keep what we have

            media_data = j["data"]["mediaData"]
            for edge in media_data.get("edges", []) or []:
                node = edge.get("node") or {}
                post = _parse_post_node(node, fallback_username=username)
                if not post:
                    report.unparsed += 1
                    continue
                if exclude_reposts and post.username.lower() != username.lower():
                    report.reposts_skipped += 1
                    continue
                # Guard against a cursor that loops back over edges we
                # already emitted.
                if post.id in seen_ids:
                    continue
                seen_ids.add(post.id)
                out.append(post)
                # Mirror into `collected` immediately: if the next request
                # raises, this post is still real output and must not be lost.
                collected.append(post)
                if limit is not None and len(out) >= limit:
                    break

            if limit is not None and len(out) >= limit:
                report.stop_reason = f"limit reached ({limit})"
                break

            # page_info parsing is defensive: a missing/renamed key
            # must not turn into an infinite walk.
            page_info = media_data.get("page_info") or {}
            if not isinstance(page_info, dict) or not page_info.get("has_next_page"):
                report.stop_reason = "the server reports no further pages"
                break
            next_cursor = page_info.get("end_cursor")
            if not next_cursor or not isinstance(next_cursor, str):
                report.stop_reason = "page_info carries no usable end_cursor"
                break
            if next_cursor in seen_cursors:
                report.stop_reason = "the server repeated the cursor"
                break  # server repeating the cursor -> stop
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            report.truncated = True
            report.stop_reason = f"page budget of {page_budget} exhausted"

        if out:
            if limit is not None:
                out = out[:limit]
            return out
        return []

    def get_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True
    ) -> list[Post]:
        username = username.lstrip("@").split("/")[0]
        # Try GraphQL; on failure (execution error) fall back to Playwright DOM
        # (robust for public profiles)
        collected: list[Post] = []
        report = ScrapeReport(limit=limit)
        self.last_report = report
        try:
            # GraphQL path — minimal, falls back to DOM on failure
            user_id = None
            try:
                user_id = self._resolve_user_id(username)
            except Exception as exc:
                # Not fatal. An unauthenticated profile page carries no
                # userID at all (Threads renders it client-side), and this
                # guard used to send every anonymous scrape straight to the
                # DOM path without ever trying discovery — which *can* resolve
                # the account on its own, because the captured variables name
                # the profile the browser actually visited.
                report.api_error = f"userID: {exc.__class__.__name__}"
            try:
                out = self._walk_graphql(
                    username,
                    user_id,
                    limit=limit,
                    exclude_reposts=exclude_reposts,
                    report=report,
                    collected=collected,
                )
            except _StaleTemplate:
                # The cached doc_id was refused. This is the whole reason
                # the template is captured rather than hardcoded: capture a
                # fresh one and walk again. Bounded by
                # _MAX_TEMPLATE_ATTEMPTS — a template that fails twice is
                # not going to fix itself.
                _warn(
                    "the cached GraphQL query id is no longer accepted; "
                    "capturing the current one and retrying"
                )
                # Nothing was collected under the dead id, and the new walk
                # starts from the first page, so the old list is dropped
                # rather than merged: the ids may not even mean the same
                # thing under a different query revision.
                collected.clear()
                try:
                    out = self._walk_graphql(
                        username,
                        user_id,
                        limit=limit,
                        exclude_reposts=exclude_reposts,
                        report=report,
                        collected=collected,
                    )
                    report.api_error = ""
                except Exception as exc:
                    # Keep the *original* rejection as the reported cause:
                    # "we could not rediscover a working id" is a detail of
                    # the recovery, while "the server refused the query" is
                    # what the user needs to read.
                    detail = exc.__class__.__name__
                    _warn(
                        f"the GraphQL query is still rejected after recapture "
                        f"({detail}); falling back"
                    )
                    out = []
                    if not report.api_error:
                        report.api_error = (
                            "GraphQL query rejected and no replacement "
                            "doc_id could be captured"
                        )

            if out:
                _finish_report(report, out)
                return out
        except Exception as exc:
            # The cursor walk can fail several pages in. Anything already
            # collected is still valid output, so keep it and only fall back to
            # the DOM scraper when we came away empty-handed.
            if collected:
                report.truncated = True
                _warn(f"GraphQL pagination stopped early ({exc.__class__.__name__})")
                _finish_report(report, collected[:limit] if limit is not None else collected)
                return collected[:limit] if limit is not None else collected
            detail = report.api_error or f"{exc.__class__.__name__}: {exc}"
            _warn(
                f"GraphQL API path unavailable ({detail}); "
                "falling back to the Playwright scraper, which is slower."
            )
        # Fallback DOM robusto
        report.used_fallback = True
        report.truncated = True  # the DOM fallback only ever sees a viewport
        posts = self._fetch_posts_via_playwright(username, limit)
        if not posts:
            # Not an exception: an empty or private profile legitimately yields
            # nothing, and that cannot be told apart from a silent failure.
            _warn(
                f"no posts found for @{username}. The account may be empty or "
                "private, or the session may lack access."
            )
        _finish_report(report, posts)
        return posts

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
