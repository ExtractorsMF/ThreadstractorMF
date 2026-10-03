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

from threadstractormf.api import ScrapeReport, ThreadsAPI
from threadstractormf.archive import (
    DownloadLedger,
    ledger_path_for,
    migrate_legacy_ledger,
)
from threadstractormf.auth import load_cookies_dict, load_netscape_cookies
from threadstractormf.downloader import (
    DownloadOutcome,
    download_media,
    download_profile_pic,
)
from threadstractormf.models import Media, Post, Profile


def _coerce_cookies(cookies: str | Path | dict[str, str] | httpx.Cookies | Any) -> Any:
    """Normalise every accepted ``cookies`` input into a CookieJar.

    A plain dict was previously stored as-is: ``get_csrf_token`` still worked
    through its fallback, but the Playwright path received a mapping where it
    expected Cookie objects and produced an *empty* cookie list, so the DOM
    scraper ran with no session at all. Normalising here means one shape for
    every consumer.
    """
    if isinstance(cookies, dict):
        return load_cookies_dict(cookies, domain=".threads.net")
    return cookies


class Threadscraper:
    def __init__(
        self,
        cookies: str | Path | httpx.Cookies | Any,
        *,
        impersonate: str | None = None,
        timeout: float = 15.0,
        rate_limit: bool = True,
        cooldown_ms: int = 2000,
        cooldown_after_100_ms: int = 120000,
        rps: float = 0.5,
        archive: bool = True,
    ):
        if isinstance(cookies, (str, Path)):
            cookies = load_netscape_cookies(cookies)
        cookies = _coerce_cookies(cookies)
        self.cookies = cookies
        # Kept so downloads use the same TLS fingerprint as the API calls:
        # a WAF that blocks the GraphQL call also blocks the CDN fetches.
        self.impersonate = impersonate
        # Download ledger, keyed by (post_id, index) so a corrected extension or
        # a different filename template never re-downloads an existing archive.
        self.archive = archive
        self._ledgers: dict[str, DownloadLedger] = {}
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

    @property
    def last_report(self) -> ScrapeReport:
        """Counters for the most recent get_posts() call."""
        return self.api.last_report

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
        adopt_existing: bool = True,
    ) -> DownloadOutcome:
        return download_media(
            media,
            dest,
            overwrite=overwrite,
            impersonate=self.impersonate,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
            username=username,
            date_iso=date_iso,
            ledger=self._ledger_for(dest),
            adopt_existing=adopt_existing,
        )

    def _ledger_for(self, dest: str | Path) -> DownloadLedger | None:
        """Ledger covering ``dest``, or None when archiving is disabled.

        One ledger per destination root — not per photos/ videos/ subdirectory —
        created and loaded on first use. Archiving is opt-in, so without it no
        ledger object is built and no directory is written.
        """
        if not self.archive:
            return None
        key = str(Path(dest).expanduser().resolve())
        ledger = self._ledgers.get(key)
        if ledger is None:
            # Carry over a pre-1.1.1 ledger before reading, so renaming the
            # bookkeeping directory does not re-download the whole library.
            migrate_legacy_ledger(key)
            ledger = DownloadLedger(ledger_path_for(key))
            ledger.load()
            self._ledgers[key] = ledger
        return ledger

    def download_many(
        self,
        medias: list[Media],
        dest: str | Path,
        *,
        overwrite: bool = False,
        filename_template: str | None = None,
        username: str | None = None,
        date_iso: str | None = None,
    ) -> list[DownloadOutcome]:
        """Download a list with built-in anti rate-limit protection.

        ``date_iso`` is passed through to the template: without it a
        ``{date:...}`` filename stamped every file with the current date.
        """
        out: list[DownloadOutcome] = []
        for m in medias:
            out.append(
                self.download(
                    m,
                    dest,
                    overwrite=overwrite,
                    filename_template=filename_template,
                    username=username,
                    date_iso=date_iso,
                )
            )
        return out

    def download_profile_pic(
        self, username: str, dest: str | Path, *, filename_template: str | None = None
    ) -> DownloadOutcome:
        profile = self.get_profile(username)
        if not profile.profile_pic_url:
            raise ValueError(f"No profile_pic_url for {username}")
        return download_profile_pic(
            profile.profile_pic_url,
            username,
            dest,
            impersonate=self.impersonate,
            limiter=self.download_limiter,
            rate_limit=self.rate_limit,
            filename_template=filename_template,
            ledger=self._ledger_for(dest),
        )

    def close(self) -> None:
        self.api.close()

    def __enter__(self) -> Threadscraper:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
