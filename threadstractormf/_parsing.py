"""Parsing helpers for Threads API responses.

Extracted from ``api.py`` to keep that module focused on orchestration (GraphQL
walk + Playwright fallback). Re-exported by ``api.py`` for backwards compat.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from threadstractormf.models import Media, MediaType, Post


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
