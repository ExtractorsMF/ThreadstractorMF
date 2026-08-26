"""Downloader — port of background.js:787-904 with post_id naming.

User rules:
  - media.id = post_id if post has a single media
  - media.id = f"{post_id}_{i}" for carousels (1-index)
  - filename = f"{media.id}.{ext}" (no datetime)
  - profile pic = f"{username}_profile.{ext}"
  - URL validated with isValidMediaUrl (CDN allowlist)
  - filename sanitized
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from threadstractormf.models import Media, sanitize_filename
from threadstractormf.rate_limit import BatchCooldownLimiter

# Allowlist same as background.js:112-116
_CDN_HINTS = ("fbcdn", "scontent", "cdninstagram", "instagram", "threads")

# Global limiter por defecto (port background.js:16-17). Se puede inyectar otro por llamada.
_default_limiter = BatchCooldownLimiter(
    cooldown_ms=2000, batch_size=100, batch_cooldown_ms=120000, jitter=0.2, enabled=True
)


def is_valid_media_url(url: str) -> bool:
    """Port background.js:96-144 isValidMediaUrl."""
    if not url or not isinstance(url, str):
        return False
    try:
        u = urlparse(url)
        if u.scheme != "https":
            return False
        host = u.netloc.lower()
        if not any(h in host for h in _CDN_HINTS):
            return False
        path = u.path.lower()
        has_ext = bool(re.search(r"\.(jpg|jpeg|png|webp|gif|mp4|webm|mov|avi)(\?|$)", path))
        has_media_path = (
            "/v/t51." in path or "/image/" in path or "/video/" in path or "/media/" in path
        )
        has_qs = bool(u.query)
        return has_ext or has_media_path or has_qs
    except Exception:
        return False


def download_media(
    media: Media,
    dest: str | Path,
    *,
    client: httpx.Client | None = None,
    overwrite: bool = False,
    limiter: BatchCooldownLimiter | None = None,
    rate_limit: bool = True,
    filename_template: str | None = None,
    username: str | None = None,
    date_iso: str | None = None,
) -> Path:
    """Download a Media to dest. Returns the final Path. Applies anti rate-limit if rate_limit=True.
    If filename_template is given, it is used to generate the filename (gallery-dl style)."""
    if not is_valid_media_url(media.url):
        raise ValueError(f"invalid download URL: {media.url}")

    dest = Path(dest).expanduser().resolve()
    # For threads All: organize into sibling folders photos/videos/profile based on media type
    # When dest is like .../<user_root> (quick scraper All), split there; otherwise use dest as is
    # Detect if this is a threads All download (dest ends with username and media has type)
    # We keep it simple: if dest name == username (or parent is username)
    # and media.type is known, create subfolder
    subfolder = None
    if media.id.endswith("_profile") and dest.name != "profile":
        subfolder = "profile"
    elif media.type == "video":
        # Only split if dest looks like a threads user root (contains username)
        # and not already in videos/photos/profile
        # Check if dest's name is username or its parent is username
        # For quick scraper All, dest is .../<user_root>,
        # so subfolder photos/videos/profile are siblings
        # For direct scrape, dest is .../posts, we still want videos sibling
        # To keep it simple for All, we create sibling folders when dest is a user root
        if dest.name not in ("photos", "videos", "profile", "posts"):
            subfolder = "videos"
    elif media.type == "image":
        if dest.name not in ("photos", "videos", "profile", "posts"):
            # For All, image goes to photos; for single posts job, keep in dest
            # (which is already photos or posts)
            # Detect if this is an All download by checking if dest contains username and not posts
            # For now, if dest is user root, split; otherwise keep as is
            if dest.name not in ("photos", "videos", "profile"):
                # Heuristic: if dest ends with username (like .../<user_root>), split
                # We check if parent is not already a content folder
                subfolder = "photos"

    if subfolder:
        dest = dest / subfolder
    dest.mkdir(parents=True, exist_ok=True)

    if filename_template:
        # For profile, ignore date template and use simple username
        if media.id.endswith("_profile") and "{date" in filename_template:
            filename = media.filename(
                template="{username}_profile.{extension}", username=username, date_iso=date_iso
            )
        else:
            filename = media.filename(
                template=filename_template, username=username, date_iso=date_iso
            )
    else:
        filename = media.filename()
    filename = sanitize_filename(filename)
    out = dest / filename
    if out.exists() and not overwrite:
        return out

    # Anti rate-limit: wait cooldown before downloading (port background.js:789-792)
    use_limiter = limiter if limiter is not None else _default_limiter
    # allow disabling globally
    should_wait = rate_limit and use_limiter.enabled
    if should_wait:
        use_limiter.wait()

    close_client = False
    if client is None:
        # fine-grained: fail fast on dead CDNs (connect 5s) but tolerate
        # slow-yet-active transfers (read timeout is per-chunk, not total)
        cdn_timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
        client = httpx.Client(follow_redirects=True, timeout=cdn_timeout, http2=True)
        close_client = True
    try:
        # Browser-like headers: video endpoints (/v/t65.*) reject python-httpx UA with 403
        cdn_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.threads.net/",
        }
        with client.stream("GET", media.url, headers=cdn_headers) as r:
            r.raise_for_status()
            with out.open("wb") as f:
                for chunk in r.iter_bytes(chunk_size=8192):
                    f.write(chunk)
    finally:
        if close_client:
            client.close()
    return out


def download_profile_pic(
    url: str,
    username: str,
    dest: str | Path,
    *,
    client: httpx.Client | None = None,
    limiter: BatchCooldownLimiter | None = None,
    rate_limit: bool = True,
    filename_template: str | None = None,
) -> Path:
    """Download profile pic with id f"{username}_profile"."""
    from threadstractormf.models import Media

    # derive extension
    ext = "jpg"
    low = url.lower()
    if ".png" in low:
        ext = "png"
    elif ".webp" in low:
        ext = "webp"
    media = Media(
        id=f"{sanitize_filename(username)}_profile",
        post_id=f"{sanitize_filename(username)}_profile",
        index=1,
        type="image",
        url=url,
        ext=ext,
    )
    # profile uses filename_template if given, else {username}_{media_id}.{ext}
    if filename_template:
        return download_media(
            media, dest, client=client, limiter=limiter, rate_limit=rate_limit,
            filename_template=filename_template, username=username,
        )
    return download_media(media, dest, client=client, limiter=limiter, rate_limit=rate_limit)
