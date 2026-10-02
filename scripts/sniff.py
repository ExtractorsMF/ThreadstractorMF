"""Sniff Threads GraphQL — discover endpoint, doc_id, headers and pagination.

Usage:
  uv run python scripts/sniff.py @user --cookies cookies.txt --limit 5

Requiere: pip install -e ".[browser]" (playwright) y cookies Netscape validas.
Saves log to scripts/sniff_log.json to implement threadstractormf/api.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, cast

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print(
        "playwright missing. Install: uv pip install -e '.[browser]'"
        " && playwright install chromium",
        file=sys.stderr,
    )
    sys.exit(2)

import typer

app = typer.Typer(add_completion=False)


@app.command()
def main(
    target: str = typer.Argument(..., help="@username o URL"),
    cookies: Path = typer.Option(..., "--cookies", "-c", exists=True),
    limit: int = typer.Option(3, "--limit", help="How many scroll rounds"),
):
    username = (
        target.strip().split("/")[-2]
        if "threads.net" in target
        else target.lstrip("@").split("/")[0]
    )
    url = f"https://www.threads.net/@{username}/media"

    # Load Netscape cookies via threadstractormf.auth
    # (tolerates spaces, HttpOnly, scrapmf-source)
    from threadstractormf._backend import to_playwright_cookies
    from threadstractormf.auth import load_netscape_cookies

    try:
        jar = load_netscape_cookies(cookies)
    except Exception as e:
        print(f"[WARN] load_netscape_cookies fallback: {e}")
        import http.cookiejar

        jar = http.cookiejar.MozillaCookieJar(str(cookies))
        jar.load(ignore_discard=True, ignore_expires=True)
    pw_cookies = to_playwright_cookies(jar)
    threads_count = sum(1 for c in jar if "threads" in getattr(c, "domain", ""))
    print(
        f"[INFO] cookies loaded: {len(pw_cookies)} (of {len(list(jar))} in jar). "
        f"Threads cookies: {threads_count}"
    )

    graphql_log: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        if pw_cookies:
            try:
                ctx.add_cookies(cast("Any", pw_cookies))
            except Exception as e:
                print(f"[WARN] add_cookies failed: {e}")
        page = ctx.new_page()

        all_requests: list[dict] = []

        def handle_request(req):
            # capture EVERYTHING to filter later; useful if Threads does not use a graphql string
            entry = {
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "postData": req.post_data,
                "resourceType": req.resource_type,
            }
            all_requests.append(entry)
            if (
                "graphql" in req.url.lower()
                or "api/graphql" in req.url.lower()
                or "bark" in req.url.lower()
                or "/api/" in req.url.lower()
            ):
                graphql_log.append(entry)
                print(f"[GRAPHQL REQ] {req.method} {req.url[:150]}")
                if req.post_data:
                    print(f"  postData: {req.post_data[:500]}")

        def handle_response(resp):
            low = resp.url.lower()
            if "graphql" in low or "api/graphql" in low or "threads.net/api" in low:
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        j = resp.json()
                        s = json.dumps(j)[:800]
                        print(f"[GRAPHQL RES] {resp.status} {resp.url[:100]} -> {s}")
                        # guardar respuesta completa
                        for e in graphql_log:
                            if e["url"] == resp.url and "response" not in e:
                                e["response"] = j
                                e["status"] = resp.status
                                break
                except Exception as e:
                    print(f"[RES ERR] {e}")

        page.on("request", handle_request)
        page.on("response", handle_response)

        print(f"Navigating {url}")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else "no resp"
            url = resp.url if resp else "none"
            print(f"goto status: {status} url: {url}")
        except Exception as e:
            print(f"[GOTO ERR] {e}")
        page.wait_for_timeout(5000)
        # scroll like content.js handleInfiniteScroll
        for i in range(limit):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(3000)
            print(f"scroll {i+1}/{limit}")
            # also try extracting media via content.js logic
            try:
                count = page.evaluate("() => document.querySelectorAll('time[datetime]').length")
                print(f"  time[datetime] count: {count}")
                imgs = page.evaluate(
                    '() => document.querySelectorAll('
                    '\'img[src*="fbcdn"], img[src*="scontent"]\').length'
                )
                print(f"  fbcdn/scontent imgs: {imgs}")
            except Exception:
                pass

        page.wait_for_timeout(3000)
        print(f"Total requests capturadas: {len(all_requests)} graphql-like: {len(graphql_log)}")
        # dump top domains si no hay graphql
        if len(graphql_log) == 0:
            from collections import Counter
            domains = Counter([r['url'].split('/')[2] for r in all_requests if '://' in r['url']])
            print("Top domains:", domains.most_common(10))
            # save all requests for debugging
            out_all = Path(__file__).with_name("sniff_all_requests.json")
            out_all.write_text(
                json.dumps(all_requests[:50], indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"All requests (50) guardado en {out_all}")

        browser.close()

    out = Path(__file__).with_name("sniff_log.json")
    out.write_text(json.dumps(graphql_log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Log guardado en {out} ({len(graphql_log)} graphql requests)")


if __name__ == "__main__":
    app()
