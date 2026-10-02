"""Threads GraphQL client — implemented via Brave sniffing of threads.com.

Sniff: POST https://www.threads.com/graphql/query
  doc_id=37598244946487292 (BarcelonaProfileMediaTabRefetchableDirectQuery)
  variables: {"after":null,"first":11,"userID":"<userID>", ...}
  headers: X-CSRFToken, X-IG-App-ID 238260118697367, X-ASBD-ID 129477,
  Referer threads.com, lsd, jazoest
  Respuesta: data.mediaData.edges[].node.thread_items[0].post
  { id, pk, code, user, caption, taken_at, image_versions2, carousel_media, video_versions }

post_id mapping: post.code if present else raw pk. Media.id = post_id / post_id_1 for carousels.
"""

from __future__ import annotations

import json
import random
import re
import sys
import time
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
from threadstractormf.auth import get_csrf_token, load_netscape_cookies
from threadstractormf.models import Media, MediaType, Post, Profile, extract_post_id
from threadstractormf.rate_limit import (
    ApiRateLimiter,
    RateLimitConfig,
    backoff_sleep,
    parse_retry_after,
)

_DOC_ID_MEDIA = "37598244946487292"
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


def _warn(message: str) -> None:
    """Diagnostic on stderr.

    stdout carries the "one line per downloaded file" contract that scrapmf
    parses, so anything informational must go to stderr instead.
    """
    print(f"threadstractormf: {message}", file=sys.stderr, flush=True)


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
        self.rate_limit_config = rate_limit_config or RateLimitConfig()
        self._api_limiter = ApiRateLimiter(
            rps=self.rate_limit_config.rps, enabled=self.rate_limit_config.enabled
        )
        self._user_id_cache: dict[str, str] = {}
        # Profile HTML is fetched by both user-id resolution and get_profile; a
        # scrape that does both should not pay for two requests.
        self._profile_cache: dict[str, str] = {}
        self._lsd: str | None = None

    def _headers(self) -> dict[str, str]:
        csrf = get_csrf_token(self.cookies) or ""
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
            "X-FB-LSD": self._lsd or "",
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

    def _fetch_posts_via_playwright(self, username: str, limit: int | None) -> list[Post]:
        from collections import defaultdict

        _require_playwright()
        from playwright.sync_api import sync_playwright

        pw = to_playwright_cookies(self.cookies)
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
            # scroll patiently: stop only after 2 consecutive empty rounds
            # (threads lazy-loads with delay; waits carry jitter for stealth)
            max_scrolls = 40
            seen_permalinks: set[str] = set()
            empty_rounds = 0
            prev_time_count = 0
            data: list[dict[str, Any]] = []
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 4000)
                scroll_wait_ms = random.uniform(1350, 2250)
                page.wait_for_timeout(scroll_wait_ms)
                time_count = page.evaluate(
                    "() => document.querySelectorAll('time[datetime]').length"
                )
                if time_count == prev_time_count:
                    empty_rounds += 1
                    if empty_rounds >= 2:
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

    def get_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True
    ) -> list[Post]:
        username = username.lstrip("@").split("/")[0]
        # Try GraphQL; on failure (execution error) fall back to Playwright DOM
        # (robust for public profiles)
        collected: list[Post] = []
        try:
            # GraphQL path — minimal, falls back to DOM on failure
            user_id = None
            try:
                user_id = self._resolve_user_id(username)
            except Exception:
                user_id = None
            if user_id:
                # GraphQL path: walk the `after` cursor until the profile is
                # exhausted or `limit` is met. A single request only ever
                # returns _PAGE_SIZE posts, so without this loop `--limit 50`
                # silently truncated to one page.
                variables = {
                    "after": None,
                    "allow_page_info_for_lox_user": False,
                    "before": None,
                    "first": _PAGE_SIZE,
                    "last": None,
                    "userID": user_id,
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
                    page_budget = min(
                        _MAX_PAGES, max(1, -(-limit // _PAGE_SIZE) + 2)
                    )
                for _ in range(page_budget):
                    variables["after"] = cursor
                    body = {
                        "av": "0",
                        "__user": "0",
                        "__a": "1",
                        "__req": "1",
                        "dpr": "1",
                        "__ccg": "EXCELLENT",
                        "__rev": "1045963118",
                        "lsd": self._lsd or "",
                        "jazoest": "22171",
                        "fb_api_caller_class": "RelayModern",
                        "fb_api_req_friendly_name": (
                            "BarcelonaProfileMediaTabRefetchableDirectQuery"
                        ),
                        "variables": json.dumps(variables),
                        "server_timestamps": "true",
                        "doc_id": _DOC_ID_MEDIA,
                    }
                    resp = self._request_with_rate_limit("POST", _GRAPHQL_URL, data=body)
                    j = resp.json()
                    if not (j.get("data") and j["data"].get("mediaData")):
                        break  # partial/absent payload: keep what we have

                    media_data = j["data"]["mediaData"]
                    for edge in media_data.get("edges", []) or []:
                        node = edge.get("node") or {}
                        post = _parse_post_node(node, fallback_username=username)
                        if not post:
                            continue
                        if exclude_reposts and post.username.lower() != username.lower():
                            continue
                        # Guard against a cursor that loops back over edges we
                        # already emitted.
                        if post.id in seen_ids:
                            continue
                        seen_ids.add(post.id)
                        out.append(post)
                        if limit is not None and len(out) >= limit:
                            break

                    if limit is not None and len(out) >= limit:
                        break

                    # Everything parsed so far survives a failure further down.
                    collected = out

                    # page_info parsing is defensive: a missing/renamed key
                    # must not turn into an infinite walk.
                    page_info = media_data.get("page_info") or {}
                    if not isinstance(page_info, dict) or not page_info.get("has_next_page"):
                        break
                    next_cursor = page_info.get("end_cursor")
                    if not next_cursor or not isinstance(next_cursor, str):
                        break
                    if next_cursor in seen_cursors:
                        break  # server repeating the cursor -> stop
                    seen_cursors.add(next_cursor)
                    cursor = next_cursor

                if out:
                    if limit is not None:
                        out = out[:limit]
                    return out
        except Exception as exc:
            # The cursor walk can fail several pages in. Anything already
            # collected is still valid output, so keep it and only fall back to
            # the DOM scraper when we came away empty-handed.
            if collected:
                _warn(f"GraphQL pagination stopped early ({exc.__class__.__name__})")
                return collected[:limit] if limit is not None else collected
            _warn(
                f"GraphQL API path unavailable ({exc.__class__.__name__}: {exc}); "
                "falling back to the Playwright scraper, which is slower."
            )
        # Fallback DOM robusto
        posts = self._fetch_posts_via_playwright(username, limit)
        if not posts:
            # Not an exception: an empty or private profile legitimately yields
            # nothing, and that cannot be told apart from a silent failure.
            _warn(
                f"no posts found for @{username}. The account may be empty or "
                "private, or the session may lack access."
            )
        return posts

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
