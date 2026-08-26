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
import re
from pathlib import Path
from typing import Any

import httpx

from threadstractormf.auth import get_csrf_token, load_netscape_cookies
from threadstractormf.models import Media, Post, Profile
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


def _guess_ext(url: str) -> str:
    u = url.lower()
    if ".mp4" in u or "video" in u:
        return "mp4"
    if ".webp" in u:
        return "webp"
    if ".png" in u:
        return "png"
    if ".gif" in u:
        return "gif"
    if ".jpeg" in u:
        return "jpeg"
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
        carousel = post.get("carousel_media")
        # single media case
        if carousel and isinstance(carousel, list) and len(carousel) > 0:
            for idx, cm in enumerate(carousel):
                # cm has image_versions2 or video_versions
                url = None
                vtype = "image"
                if cm.get("video_versions"):
                    vv = cm["video_versions"]
                    if isinstance(vv, list) and vv:
                        url = vv[0].get("url")
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
                vv = post["video_versions"]
                if isinstance(vv, list) and vv:
                    url = vv[0].get("url")
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


class ThreadsAPI:
    """Low-level client. Used by Threadscraper."""

    def __init__(
        self,
        cookies: httpx.Cookies | Any | str | Path,
        *,
        impersonate: str | None = None,
        timeout: float = 30.0,
        rate_limit_config: RateLimitConfig | None = None,
    ):

        if isinstance(cookies, (str, Path)):
            cookies = load_netscape_cookies(cookies)
        self.cookies = cookies
        self.impersonate = impersonate
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self.rate_limit_config = rate_limit_config or RateLimitConfig()
        self._api_limiter = ApiRateLimiter(
            rps=self.rate_limit_config.rps, enabled=self.rate_limit_config.enabled
        )
        self._user_id_cache: dict[str, str] = {}
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

    def _get_client(self) -> httpx.Client:
        if self._client:
            return self._client
        self._client = httpx.Client(
            cookies=self.cookies,
            headers=self._headers(),
            timeout=self.timeout,
            follow_redirects=True,
            http2=True,
        )
        return self._client

    def _request_with_rate_limit(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Wrapper with ApiRateLimiter + Retry-After + exponential backoff for 429/5xx."""
        client = self._get_client()
        if self.rate_limit_config.enabled:
            self._api_limiter.wait()
        last_exc: Exception | None = None
        for attempt in range(self.rate_limit_config.max_retries + 1):
            try:
                resp = client.request(method, url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry = parse_retry_after(dict(resp.headers))
                    if retry is not None:
                        import time

                        time.sleep(retry)
                    elif attempt < self.rate_limit_config.max_retries:
                        backoff_sleep(attempt, base=self.rate_limit_config.backoff_base)
                    else:
                        resp.raise_for_status()
                    if attempt < self.rate_limit_config.max_retries:
                        continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                last_exc = e
                if attempt < self.rate_limit_config.max_retries:
                    retry = parse_retry_after(dict(e.response.headers)) if e.response else None
                    if retry is not None:
                        import time

                        time.sleep(retry)
                    else:
                        backoff_sleep(attempt, base=self.rate_limit_config.backoff_base)
                    continue
                raise
            except httpx.RequestError as e:
                last_exc = e
                if attempt < self.rate_limit_config.max_retries:
                    backoff_sleep(attempt, base=self.rate_limit_config.backoff_base)
                    continue
                raise
        if last_exc:
            raise last_exc
        raise RuntimeError("request failed without exception")

    def _resolve_user_id(self, username: str) -> str:
        if username in self._user_id_cache:
            return self._user_id_cache[username]
        # Try via HTML using lsd/csrf
        try:
            resp = self._request_with_rate_limit("GET", f"https://www.threads.com/@{username}")
            html = resp.text
            # buscar userID en html: "userID":"<userID>"
            m = re.search(r'"userID"\s*:\s*"(\d+)"', html)
            if m:
                uid = m.group(1)
                self._user_id_cache[username] = uid
                # extraer lsd
                m2 = re.search(r'"lsd"\s*:\s*"([^"]+)"', html)
                if m2:
                    self._lsd = m2.group(1)
                return uid
            # fallback: data.userData
            m = re.search(r'"pk"\s*:\s*"(\d+)"', html)
            if m:
                uid = m.group(1)
                self._user_id_cache[username] = uid
                return uid
        except Exception:
            pass
        # fallback: raise if no resolved ID (no hardcoded IDs)
        raise ValueError(f"could not resolve userID for @{username}")

    def get_profile(self, username: str) -> Profile:
        username = username.lstrip("@").split("/")[0]
        # Try Playwright DOM first if threads.com cookies present, else HTML regex
        try:
            # quick html lookup for profile pic
            resp = self._request_with_rate_limit("GET", f"https://www.threads.com/@{username}")
            html = resp.text
            m = re.search(r'"profile_pic_url"\s*:\s*"([^"]+)"', html)
            pic = None
            if m:
                pic = m.group(1).replace(r"\u0026", "&")
            # if no pic found, fall back to Playwright DOM
            if not pic:
                try:
                    pic = self._fetch_profile_pic_via_playwright(username)
                except Exception:
                    pass
            uid = self._user_id_cache.get(username)
            if not uid:
                # extract from html if present
                m2 = re.search(r'"userID"\s*:\s*"(\d+)"', html)
                if m2:
                    uid = m2.group(1)
                else:
                    # look for the user pk in edge data
                    m3 = re.search(r'"pk"\s*:\s*"(\d+)"', html)
                    if m3:
                        uid = m3.group(1)
            from threadstractormf.models import sanitize_filename

            profile_pic_media = None
            if pic:
                from threadstractormf.models import Media

                profile_pic_media = Media(
                    id=f"{sanitize_filename(username)}_profile",
                    post_id=f"{sanitize_filename(username)}_profile",
                    index=1,
                    type="image",
                    url=pic,
                    ext=_guess_ext(pic),
                )
            return Profile(
                username=username,
                user_id=uid,
                profile_pic_url=pic,
                profile_pic_media=profile_pic_media,
            )
        except Exception as e:
            raise RuntimeError(f"get_profile failed for @{username}: {e}") from e

    def _fetch_profile_pic_via_playwright(self, username: str) -> str | None:
        try:
            from playwright.sync_api import sync_playwright

            # convertir jar a pw
            pw = []
            for c in self.cookies:
                pw.append(
                    {
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain.lstrip("."),
                        "path": c.path,
                        "secure": bool(c.secure),
                    }
                )
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True, args=["--no-sandbox"])
                ctx = b.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    )
                )
                if pw:
                    ctx.add_cookies(pw)
                page = ctx.new_page()
                page.goto(f"https://www.threads.com/@{username}", wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                pic = page.evaluate(
                    """() => document.querySelector('img[src*="t51.2885-19"]')?.src
                        || document.querySelector('img[src*="s150x150"][src*="profile"]')?.src
                        || null"""
                )
                b.close()
                return pic
        except Exception:
            return None

    def _fetch_posts_via_playwright(self, username: str, limit: int | None) -> list[Post]:
        from playwright.sync_api import sync_playwright

        pw = []
        for c in self.cookies:
            try:
                pw.append(
                    {
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain.lstrip("."),
                        "path": c.path,
                        "secure": bool(c.secure),
                    }
                )
            except Exception:
                continue
        posts: list[Post] = []
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = b.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            )
            if pw:
                try:
                    ctx.add_cookies(pw)
                except Exception:
                    pass
            page = ctx.new_page()
            page.goto(f"https://www.threads.com/@{username}/media", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            # scroll up to limit or 30 like content.js
            max_scrolls = 30
            seen_permalinks: set[str] = set()
            for _ in range(max_scrolls):
                page.mouse.wheel(0, 3500)
                page.wait_for_timeout(3000)
                # check if new posts loaded
                data = page.evaluate(r"""() => {
                    const posts=[];
                    document.querySelectorAll('time[datetime]').forEach(t=>{
                        const a=t.closest('a[href*="/post/"]');
                        const permalink=a?a.href:null;
                        if(!permalink) return;
                        const dt=t.getAttribute('datetime');
                        let container=t;
                        let media_urls=[];
                        let raw_html="";
                        let postCode = (permalink.match(/\/post\/([^/?#]+)/) || [])[1] || "";
                        for(let i=0;i<10 && media_urls.length==0;i++){
                            let els=[...container.querySelectorAll('img, video, source')];
                            media_urls=els.filter(el=>{
                                const s=el.src||el.srcset||"";
                                if(!s) return false;
                                if(s.includes('t51.2885-19')) return false;
                                return s.includes('fbcdn')||s.includes('scontent')
                                    ||s.includes('cdninstagram')||s.includes('.mp4');
                            }).map(el=>el.src||el.srcset.split(' ')[0]);
                            raw_html = container.innerHTML;
                            // Always also scan for video_versions in this container's HTML
                            const re2 = /"video_versions":\s*\[\s*\{"url":"([^"]+)"/g;
                            let m2;
                            while((m2=re2.exec(raw_html))!==null){
                                const u=m2[1].replaceAll(/\\u0026/g,"&").replaceAll(/\\u0025/g,"%");
                                if(u && !media_urls.includes(u)) media_urls.push(u);
                            }
                            if(media_urls.length>0) break;
                            container=container.parentElement;
                            if(!container) break;
                        }
                        // Final fallback: look in the whole document for this post's
                        // code's video_versions (grid shows poster jpgs for videos,
                        // the mp4 is in the post's JSON elsewhere)
                        if(postCode){
                            const fullHtml=document.documentElement.innerHTML;
                            const idx=fullHtml.indexOf(postCode);
                            if(idx!==-1){
                                const snippet=fullHtml.slice(Math.max(0,idx-8000), idx+12000);
                                const re3 = /"video_versions":\s*\[\s*\{"url":"([^"]+)"/g;
                                let m3;
                                while((m3=re3.exec(snippet))!==null){
                                    const u=m3[1].replaceAll(/\\u0026/g,"&")
                                        .replaceAll(/\\u0025/g,"%");
                                    if(u && !media_urls.includes(u)) media_urls.push(u);
                                }
                            }
                        }
                        if(media_urls.length===0 && raw_html){
                            const re = /"video_versions":\s*\[\s*\{"url":"([^"]+)"/g;
                            let m;
                            while((m=re.exec(raw_html))!==null){
                                const u=m[1].replaceAll(/\\u0026/g,"&").replaceAll(/\\u0025/g,"%");
                                if(u) media_urls.push(u);
                            }
                        }
                        posts.push({permalink, datetime:dt, media_urls: [...new Set(media_urls)]});
                    });
                    return posts;
                }""")
                # Strict filter: only posts whose permalink contains /@<target>/ (exact user)
                target_lower = username.lower()
                data = [d for d in data if f"/@{target_lower}/" in d["permalink"].lower()]
                # For carousels that appear as all-images but are actually mixed,
                # fetch detail via Playwright
                for d in list(data):
                    if d["media_urls"] and all(
                        not u.lower().endswith(".mp4") for u in d["media_urls"]
                    ):
                        try:
                            # Quick httpx check for video_versions in the post
                            # detail (fast, no browser)
                            try:
                                resp = self._request_with_rate_limit("GET", d["permalink"])
                                if '"video_versions"' not in resp.text:
                                    continue
                            except Exception:
                                # If httpx fails, fall back to Playwright check
                                m_code = __import__("re").search(r"/post/([^/?#]+)", d["permalink"])
                                code = m_code.group(1) if m_code else ""
                                has_video = page.evaluate(
                                    """(code) => {
                                        const html=document.documentElement.innerHTML;
                                        const idx=html.indexOf(code);
                                        if(idx===-1) return false;
                                        return html.slice(Math.max(0,idx-5000), idx+10000)
                                            .includes('video_versions');
                                    }""",
                                    code,
                                )
                                if not has_video:
                                    continue
                            page.goto(d["permalink"], wait_until="domcontentloaded")
                            page.wait_for_timeout(2500)
                            detail_urls = page.evaluate(r"""() => {
                                const urls=[];
                                document.querySelectorAll('img, video, source').forEach(el=>{
                                    const s=el.src||el.srcset||"";
                                    if(!s) return;
                                    if(s.includes('t51.2885-19')) return;
                                    if(s.includes('fbcdn')||s.includes('scontent')||s.includes('cdninstagram')||s.includes('.mp4')){
                                        const first = s.split(',')[0].split(' ')[0].trim();
                                        if(first) urls.push(first);
                                    }
                                });
                                const html=document.documentElement.innerHTML;
                                const re = /"video_versions":\s*\[\s*\{"url":"([^"]+)"/g;
                                let m2;
                                while((m2=re.exec(html))!==null){
                                    const u=m2[1].replaceAll(/\\u0026/g,"&")
                                        .replaceAll(/\\u0025/g,"%");
                                    if(u && !urls.includes(u)) urls.push(u);
                                }
                                return [...new Set(urls)];
                            }""")
                            vids = [u for u in detail_urls if u.lower().endswith(".mp4")]
                            if vids:
                                # Add the videos to this post's media
                                combined = list(d["media_urls"])
                                seen = set(combined)
                                for u in vids:
                                    if u not in seen:
                                        seen.add(u)
                                        combined.append(u)
                                d["media_urls"] = combined[:10]
                        except Exception:
                            pass
                # Return to media tab
                try:
                    page.goto(
                        f"https://www.threads.com/@{username}/media",
                        wait_until="domcontentloaded",
                    )
                    page.wait_for_timeout(2000)
                except Exception:
                    pass
                # deduplicate
                new_posts = []
                for d in data:
                    if d["permalink"] not in seen_permalinks:
                        seen_permalinks.add(d["permalink"])
                        new_posts.append(d)
                # convert to Post
                for d in new_posts:
                    m = re.search(r"/post/([^/?#]+)", d["permalink"])
                    pid = m.group(1) if m else None
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
                if len(new_posts) == 0:
                    # no new
                    break
            b.close()
        if limit is not None:
            posts = posts[:limit]
        # filtro reposts ya implicito por username == target (DOM already)
        return posts

    def get_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True
    ) -> list[Post]:
        username = username.lstrip("@").split("/")[0]
        # Try GraphQL; on failure (execution error) fall back to Playwright DOM
        # (robust for public profiles)
        try:
            # GraphQL path — minimal, falls back to DOM on failure
            user_id = None
            try:
                user_id = self._resolve_user_id(username)
            except Exception:
                user_id = None
            if user_id:
                # try it with rate limiting, but if data.mediaData is None, fall back
                variables = {
                    "after": None,
                    "allow_page_info_for_lox_user": False,
                    "before": None,
                    "first": 12,
                    "last": None,
                    "userID": user_id,
                    "__relay_internal__pv__BarcelonaIsLoggedInrelayprovider": False,
                }
                data = {
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
                    "fb_api_req_friendly_name": "BarcelonaProfileMediaTabRefetchableDirectQuery",
                    "variables": json.dumps(variables),
                    "server_timestamps": "true",
                    "doc_id": _DOC_ID_MEDIA,
                }
                resp = self._request_with_rate_limit("POST", _GRAPHQL_URL, data=data)
                j = resp.json()
                if j.get("data") and j["data"].get("mediaData"):
                    out: list[Post] = []
                    media_data = j["data"]["mediaData"]
                    edges = media_data.get("edges", [])
                    for edge in edges:
                        node = edge.get("node") or {}
                        post = _parse_post_node(node, fallback_username=username)
                        if not post:
                            continue
                        if exclude_reposts and post.username.lower() != username.lower():
                            continue
                        out.append(post)
                        if limit is not None and len(out) >= limit:
                            break
                    if out:
                        if limit is not None:
                            out = out[:limit]
                        return out
        except Exception:
            pass
        # Fallback DOM robusto
        return self._fetch_posts_via_playwright(username, limit)

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
