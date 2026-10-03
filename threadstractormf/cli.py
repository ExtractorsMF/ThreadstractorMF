"""gallery-dl / yt-dlp style CLI — entry point: threadstractormf."""

from __future__ import annotations

import http.cookiejar
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from threadstractormf import __version__
from threadstractormf.api import ScrapeReport
from threadstractormf.auth import load_netscape_cookies
from threadstractormf.downloader import DownloadOutcome, DownloadStatus

try:  # browser_cookie3 is an optional extra
    from browser_cookie3 import BrowserCookieError as _BrowserCookieError
except ImportError:  # pragma: no cover

    class _BrowserCookieError(Exception):  # type: ignore[no-redef]
        pass

app = typer.Typer(
    add_completion=False,
    help="threadstractormf — Threads downloader (post_id naming, gallery-dl cookies)",
)
console = Console()
err_console = Console(stderr=True)


# stdout carries the "one line per downloaded file" contract that scrapmf counts,
# so nothing printed to it may be wrapped. Rich wraps at the terminal width
# (80 columns when piped), which silently turned a single 600-character CDN URL
# into eight lines and made an orchestrator count eight files instead of one.
# ``soft_wrap=True`` disables that for the lines we emit.


def _count(outcome: DownloadOutcome, report: ScrapeReport) -> None:
    """Tally one media outcome so the summary can tell the truth."""
    if outcome.status is DownloadStatus.DOWNLOADED:
        report.downloaded += 1
    else:
        report.skipped += 1


def _report_scrape(report: ScrapeReport) -> None:
    """Print what the scrape actually found, and what it could not.

    Goes to stderr: stdout is the "one line per downloaded file" contract, and
    a summary there would be counted as a file by scrapmf. Before this, a
    scrape that returned 4 posts out of a 60-post profile ended in complete
    silence and was indistinguishable from a full one.
    """
    parts = [
        f"posts: {report.posts}",
        f"media: {report.media}",
        f"downloaded: {report.downloaded}",
        f"skipped: {report.skipped}",
        f"failed: {report.failed}",
    ]
    if report.no_media:
        parts.append(f"without media: {report.no_media}")
    if report.reposts_skipped:
        parts.append(f"reposts excluded: {report.reposts_skipped}")
    if report.unparsed:
        parts.append(f"unparseable: {report.unparsed}")
    err_console.print("summary — " + " | ".join(parts), soft_wrap=True)

    if report.api_error:
        err_console.print(
            f"warning — the API did not respond as expected: {report.api_error}",
            soft_wrap=True,
        )
    if report.used_fallback:
        err_console.print(
            "warning — the browser fallback was used: it is slower than the "
            "API, but it reads the public profile in full.",
            soft_wrap=True,
        )
    if report.truncated:
        err_console.print(
            f"warning — the walk stopped before exhausting the profile "
            f"({report.stop_reason or 'unknown reason'}).",
            soft_wrap=True,
        )
    if report.failed:
        err_console.print(
            f"warning — {report.failed} media item(s) could not be downloaded.",
            soft_wrap=True,
        )


def _version_callback(value: bool) -> None:
    """Print the version and exit.

    Eager and callback-style rather than a plain option in main(): the target
    argument is required, so a plain flag could not run before typer rejected
    the missing argument. A callback with is_eager runs first and exits.
    """
    if value:
        # __version__ is what semantic-release rewrites (version_variables in
        # pyproject.toml), so this can never drift from the released build.
        console.print(f"threadstractormf {__version__}", soft_wrap=True)
        raise typer.Exit()


@app.command()
def main(
    target: str = typer.Argument(..., help="URL https://www.threads.net/@user/media or @username"),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the installed version and exit",
    ),
    cookies: Path | None = typer.Option(
        None, "--cookies", "-c", help="Netscape cookies.txt (gallery-dl compatible)"
    ),
    cookies_from_browser: str | None = typer.Option(
        None,
        "--cookies-from-browser",
        help="brave|chrome|firefox (reads local DB, like gallery-dl)",
    ),
    dest: Path = typer.Option(Path("./dl"), "--dest", "-d", help="Output directory"),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Posts limit (default all)"),
    profile_pic_only: bool = typer.Option(False, "--profile-pic-only", help="Profile pic only"),
    photos_only: bool = typer.Option(False, "--photos-only", help="Photos only"),
    videos_only: bool = typer.Option(False, "--videos-only", help="Videos only"),
    impersonate: str | None = typer.Option(
        None, "--impersonate", help="chrome (requires curl_cffi to be installed)"
    ),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing files"),
    no_rate_limit: bool = typer.Option(
        False, "--no-rate-limit", help="Disable anti rate-limit (not recommended)"
    ),
    cooldown: int = typer.Option(
        2000, "--cooldown", help="Cooldown between downloads ms (default 2000, same as extension)"
    ),
    batch_cooldown: int = typer.Option(
        120000, "--batch-cooldown", help="Cooldown every 100 downloads ms (default 120000)"
    ),
    rps: float = typer.Option(0.5, "--rps", help="Requests/s for GraphQL API (default 0.5)"),
    filename_template: str | None = typer.Option(
        None,
        "--filename-template",
        help="Template filename like gallery-dl: {date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}",
    ),
    directory_template: str | None = typer.Option(
        None,
        "--directory-template",
        help="Template directory, e.g. {scrapmf_root}/{category}/{username}/{subcategory}",
    ),
    archive: bool = typer.Option(
        False,
        "--archive",
        help="Keep a ledger of downloads in <dest>/.archive so re-runs skip them",
    ),
    no_adopt_existing: bool = typer.Option(
        False,
        "--no-adopt-existing",
        help="Do not adopt files already on disk under an older name",
    ),
    get_urls: bool = typer.Option(
        False,
        "--get-urls",
        help="Print the media URLs instead of downloading (dry run)",
    ),
) -> None:
    """Download Threads media by post_id."""
    # normalize @user — supports threads.net and threads.com, URL or @handle
    import re as _re

    from threadstractormf.client import Threadscraper

    if "threads.com" in target or "threads.net" in target:
        m = _re.search(r"/@([^/]+)", target)
        if m:
            username = m.group(1)
        else:
            # fallback: last non-empty segment
            username = target.strip().rstrip("/").split("/")[-1].lstrip("@")
    else:
        username = target.lstrip("@").split("/")[0]
    if not username:
        console.print("[red]Invalid target. Use @username or URL[/red]")
        raise typer.Exit(1)

    # Validate --impersonate before any work. The check otherwise only runs when the
    # first download builds the client, so a typo surfaced minutes later — or
    # never, on a profile with no media.
    if impersonate:
        from threadstractormf._backend import known_impersonate_targets

        if impersonate not in known_impersonate_targets():
            err_console.print(
                f"[red]unknown --impersonate target: {impersonate!r}[/red]\n"
                f"Valid targets include: "
                f"{', '.join(sorted(known_impersonate_targets())[:8])}"
            )
            raise typer.Exit(1)

    # Load cookies before touching the network, and report a bad file, a missing
    # browser or an unknown browser name as a plain message instead of a
    # traceback: exporting cookies as JSON is the most common mistake, and the
    # LoadError text is what explains it.
    cookies_arg: Any
    try:
        if cookies_from_browser:
            from threadstractormf.auth import load_from_browser

            cookies_arg = load_from_browser(cookies_from_browser)
        elif cookies is not None:
            if not cookies.exists():
                err_console.print(f"[red]cookies file not found: {cookies}[/red]")
                raise typer.Exit(1)
            cookies_arg = load_netscape_cookies(cookies)
        else:
            err_console.print(
                "[red]--cookies or --cookies-from-browser required "
                "(gallery-dl compatible)[/red]"
            )
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except (
        http.cookiejar.LoadError,
        OSError,
        ValueError,
        # browser_cookie3.BrowserCookieError subclasses Exception directly, so it
        # is not caught by the OSError/ValueError branches above; its message is
        # the actionable one ("Failed to find cookies for Chrome browser").
        _BrowserCookieError,
    ) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    except ImportError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    scraper = Threadscraper(
        cookies=cookies_arg,
        impersonate=impersonate,
        rate_limit=not no_rate_limit,
        cooldown_ms=cooldown,
        cooldown_after_100_ms=batch_cooldown,
        rps=rps,
        archive=archive,
    )
    adopt_existing = not no_adopt_existing
    # resolve directory_template -> final dest
    final_dest = dest
    if directory_template:
        from threadstractormf.template import render_directory

        # scrapmf passes scrapmf_root/profile; default used otherwise
        subcat = "profile" if profile_pic_only else "posts"
        rendered = render_directory(
            directory_template, username=username, category="threads", subcategory=subcat
        )
        # si dest es ~/Descargas y template es {scrapmf_root}/threads/... lo unimos
        # If template is already absolute, use it as-is
        if rendered.startswith("/"):
            final_dest = Path(rendered)
        else:
            # For All, rendered is "<user_root>" without '/', goes to dest/<user_root>
            final_dest = dest / rendered if rendered else dest
        # Defence in depth. render_directory already reduces {username} to a
        # single component, but a relative template must not be able to walk out
        # of --dest either way: an absolute template is a deliberate choice and
        # is honoured, a relative one may not escape. Catching it here turns a
        # silent write into an error, instead of files landing one directory up.
        if not rendered.startswith("/"):
            root_dir = dest.expanduser().resolve()
            if not final_dest.resolve().is_relative_to(root_dir):
                err_console.print(
                    f"[red]--directory-template resolves outside --dest: "
                    f"{final_dest.resolve()} (escapes {root_dir})[/red]"
                )
                raise typer.Exit(1)
        if not get_urls:
            # A dry run must leave the filesystem untouched, so it does not
            # create the directory tree it would have written into.
            final_dest.mkdir(parents=True, exist_ok=True)

    try:
        if profile_pic_only:
            if get_urls:
                # One URL per line, same shape as the per-file lines scrapmf
                # counts, so a dry run reads the same way.
                profile = scraper.get_profile(username)
                if profile.profile_pic_url:
                    console.print(profile.profile_pic_url, soft_wrap=True)
                return
            out = scraper.download_profile_pic(
                username, final_dest, filename_template=filename_template
            )
            console.print(f"[green]profile pic -> {out}[/green]", soft_wrap=True)
            return
        posts = scraper.get_posts(username, limit=limit)
        report = scraper.last_report
        # For All (no filter), also download profile pic after posts as sibling
        is_all = not photos_only and not videos_only and not profile_pic_only
        if is_all and not get_urls:
            try:
                outcome = scraper.download_profile_pic(
                    username, final_dest, filename_template=None
                )
                _count(outcome, report)
                console.print(f"  {username}_profile -> {outcome.path}", soft_wrap=True)
            except Exception:
                pass
        for post in posts:
            for media in post.media:
                if photos_only and media.type != "image":
                    continue
                if videos_only and media.type != "video":
                    continue
                if get_urls:
                    # Dry run: print the URL and move on, leaving the pacing
                    # limiter alone (a scrape still must not hammer the API).
                    console.print(media.url, soft_wrap=True)
                    continue
                # media.id is already post_id or post_id_N,
                # with configurable template for chronological ordering
                try:
                    outcome = scraper.download(
                        media,
                        final_dest,
                        overwrite=overwrite,
                        filename_template=filename_template,
                        username=username,
                        date_iso=post.datetime_iso,
                        adopt_existing=adopt_existing,
                    )
                    _count(outcome, report)
                    console.print(f"  {media.id} -> {outcome.path}", soft_wrap=True)
                except Exception as e:
                    report.failed += 1
                    # un media fallido (URL firmada expirada, 403 CDN, red)
                    # must not abort the whole batch. Failures go to STDERR
                    # (stdout is the "one line per downloaded file" contract
                    # with orchestrators like scrapmf — a failed line there
                    # would be miscounted as a successful download).
                    err_console.print(f"[yellow]  {media.id} failed: {e}[/yellow]")
        _report_scrape(report)
    finally:
        scraper.close()


if __name__ == "__main__":
    app()
