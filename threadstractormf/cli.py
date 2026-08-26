"""gallery-dl / yt-dlp style CLI — entry point: threadstractormf."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

app = typer.Typer(
    add_completion=False,
    help="threadstractormf — Threads downloader (post_id naming, gallery-dl cookies)",
)
console = Console()


@app.command()
def main(
    target: str = typer.Argument(..., help="URL https://www.threads.net/@user/media or @username"),
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
        None, "--impersonate", help="chrome (usa curl_cffi si instalado)"
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
        help="Template directory, ej {scrapmf_root}/{category}/{username}/{subcategory}",
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

    # resolve cookies: --cookies takes priority, else --cookies-from-browser
    if cookies_from_browser:
        from threadstractormf.auth import load_from_browser

        cookies_arg = load_from_browser(cookies_from_browser)
    elif cookies is not None:
        if not cookies.exists():
            console.print(f"[red]cookies file not found: {cookies}[/red]")
            raise typer.Exit(1)
        cookies_arg = cookies
    else:
        console.print(
            "[red]--cookies or --cookies-from-browser required (gallery-dl compatible)[/red]"
        )
        raise typer.Exit(1)

    scraper = Threadscraper(
        cookies=cookies_arg,
        impersonate=impersonate,
        rate_limit=not no_rate_limit,
        cooldown_ms=cooldown,
        cooldown_after_100_ms=batch_cooldown,
        rps=rps,
    )
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
        final_dest.mkdir(parents=True, exist_ok=True)

    try:
        if profile_pic_only:
            out = scraper.download_profile_pic(
                username, final_dest, filename_template=filename_template
            )
            console.print(f"[green]profile pic -> {out}[/green]")
            return
        posts = scraper.get_posts(username, limit=limit)
        # For All (no filter), also download profile pic after posts as sibling
        is_all = not photos_only and not videos_only and not profile_pic_only
        if is_all:
            try:
                out = scraper.download_profile_pic(username, final_dest, filename_template=None)
                console.print(f"  {username}_profile -> {out}")
            except Exception:
                pass
        for post in posts:
            for media in post.media:
                if photos_only and media.type != "image":
                    continue
                if videos_only and media.type != "video":
                    continue
                # media.id is already post_id or post_id_N,
                # with configurable template for chronological ordering
                try:
                    out = scraper.download(
                        media,
                        final_dest,
                        overwrite=overwrite,
                        filename_template=filename_template,
                        username=username,
                        date_iso=post.datetime_iso,
                    )
                    console.print(f"  {media.id} -> {out}")
                except Exception as e:
                    # un media fallido (URL firmada expirada, 403 CDN, etc.)
                    # must not abort the whole batch
                    console.print(f"[yellow]  {media.id} failed: {e}[/yellow]")
    except NotImplementedError as e:
        console.print(f"[yellow]{e}[/yellow]")
        console.print(
            "[dim]Hint: run sniff first:"
            " uv run python scripts/sniff.py @user --cookies cookies.txt[/dim]"
        )
        raise typer.Exit(2) from e
    finally:
        scraper.close()


if __name__ == "__main__":
    app()
