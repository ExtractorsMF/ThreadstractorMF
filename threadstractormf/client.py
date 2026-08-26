"""Main facade — import threadstractormf / Threadscraper.

Usage:
    from threadstractormf import Threadscraper
    scraper = Threadscraper(cookies="cookies.txt")
    posts = scraper.get_posts("user", limit=50)
    scraper.download(posts[0].media[0], dest="./dl")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from threadstractormf.api import ThreadsAPI
from threadstractormf.auth import load_netscape_cookies
from threadstractormf.downloader import download_media, download_profile_pic
from threadstractormf.models import Media, Post, Profile


class Threadscraper:
    def __init__(
        self,
        cookies: str | Path | httpx.Cookies | Any,
        *,
        impersonate: str | None = None,
        timeout: float = 30.0,
        rate_limit: bool = True,
        cooldown_ms: int = 2000,
        cooldown_after_100_ms: int = 120000,
        rps: float = 0.5,
    ):
        if isinstance(cookies, (str, Path)):
            cookies = load_netscape_cookies(cookies)
        self.cookies = cookies
        from threadstractormf.rate_limit import BatchCooldownLimiter, RateLimitConfig

        self.rate_limit = rate_limit
        self.download_limiter = BatchCooldownLimiter(
            cooldown_ms=cooldown_ms if rate_limit else 0,
            batch_size=100,
            batch_cooldown_ms=cooldown_after_100_ms if rate_limit else 0,
            enabled=rate_limit,
        )
        cfg = RateLimitConfig(
            cooldown_ms=cooldown_ms,
            cooldown_after_100_ms=cooldown_after_100_ms,
            rps=rps,
            enabled=rate_limit,
        )
        self.api = ThreadsAPI(
            cookies, impersonate=impersonate, timeout=timeout, rate_limit_config=cfg
        )

    # --- API delegada ---
    def get_profile(self, username: str) -> Profile:
        return self.api.get_profile(username)

    def get_posts(
        self, username: str, *, limit: int | None = None, exclude_reposts: bool = True
    ) -> list[Post]:
        return self.api.get_posts(username, limit=limit, exclude_reposts=exclude_reposts)

    # --- download ---
    def download(
        self,
        media: Media,
        dest: str | Path,
        *,
        overwrite: bool = False,
        filename_template: str | None = None,
        username: str | None = None,
        date_iso: str | None = None,
    ) -> Path:
        return download_media(
            media,
            dest,
            overwrite=overwrite,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
            username=username,
            date_iso=date_iso,
        )

    def download_many(
        self,
        medias: list[Media],
        dest: str | Path,
        *,
        overwrite: bool = False,
        filename_template: str | None = None,
        username: str | None = None,
    ) -> list[Path]:
        """Download a list with built-in anti rate-limit protection."""
        out: list[Path] = []
        for m in medias:
            # if username not passed, could infer from media.post_id? better explicit
            out.append(
                self.download(
                    m,
                    dest,
                    overwrite=overwrite,
                    filename_template=filename_template,
                    username=username,
                )
            )
        return out

    def download_profile_pic(
        self, username: str, dest: str | Path, *, filename_template: str | None = None
    ) -> Path:
        profile = self.get_profile(username)
        if not profile.profile_pic_url:
            raise ValueError(f"No profile_pic_url for {username}")
        return download_profile_pic(
            profile.profile_pic_url,
            username,
            dest,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
        )

    def close(self) -> None:
        self.api.close()

    def __enter__(self) -> Threadscraper:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
