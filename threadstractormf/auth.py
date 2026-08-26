"""Auth via Netscape cookies.txt — 100% compatible with gallery-dl / yt-dlp.

gallery-dl and yt-dlp load cookies with http.cookiejar.MozillaCookieJar
which requires:
  - Line 1: "# Netscape HTTP Cookie File" or "# HTTP Cookie File"
  - 7 fields separated by TAB: domain\\tTRUE/FALSE\\tpath\\tTRUE/FALSE\\texpires\\tname\\tvalue
  - Domain with "." prefix (e.g. .threads.net) = sent to subdomains (TRUE)
  - Expiry 0 = session cookie

This module replicates that behavior so the same cookies.txt works
in threadstractormf, gallery-dl and yt-dlp without conversion.
"""

from __future__ import annotations

import http.cookiejar
import tempfile
from pathlib import Path

import httpx


def _normalize_netscape_file(path: Path) -> Path:
    """Normalize Netscape file for gallery-dl compatibility:
    - Removes scrapmf-source line if present (not MAGIC)
    - Converts multiple spaces to TABs (many generators use spaces)
    - Handles #HttpOnly_ prefix
    - Skips empty lines
    Returns normalized temporary Path.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()
    # Find MAGIC header
    magic_idx = -1
    for i, line in enumerate(lines):
        if "HTTP Cookie File" in line:
            magic_idx = i
            break
    if magic_idx == -1:
        raise http.cookiejar.LoadError(f"{path} does not look like a Netscape format cookies file")
    # Rebuild from MAGIC onwards
    out_lines: list[str] = []
    out_lines.append(lines[magic_idx].strip())
    for raw_line in lines[magic_idx + 1 :]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and "HTTP Cookie File" in stripped:
            continue
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            # regular comment: keep pure comments?
            # gallery-dl ignores them, we skip them
            if stripped.startswith("# This"):
                continue
            # other comments: ignore
            if stripped.startswith("#"):
                # if it is #HttpOnly_, processed below
                if not stripped.startswith("#HttpOnly_"):
                    continue
        # Normalize #HttpOnly_
        line = raw_line
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        # If line still starts with #, it is a comment -> skip
        if line.strip().startswith("#"):
            continue
        # Split by TAB or by 2+ spaces
        # MozillaCookieJar requires TABs, so we convert
        # First split by TAB if tabs are present
        if "\t" in line:
            parts = line.split("\t")
        else:
            # split by whitespace 2+ or \s+ but preserving values with spaces?
            # value may contain spaces / JSON -> use maxsplit=6
            import re

            parts = re.split(r"\s+", line.strip(), maxsplit=6)
        if len(parts) != 7:
            # malformed line, skip silently (gallery-dl does the same)
            continue
        domain, flag, cpath, secure, expires, name, value = parts
        # Rebuild with TABs and without #HttpOnly_ prefix (already removed)
        out_lines.append("\t".join([domain, flag, cpath, secure, expires, name, value]))
    # Write temp file
    tmp = Path(tempfile.mktemp(suffix=".cookies.txt"))
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return tmp


def load_netscape_cookies(
    path: str | Path, *, ignore_expires: bool = True, ignore_discard: bool = True
) -> http.cookiejar.MozillaCookieJar:
    """Load a Netscape cookies.txt and return a CookieJar ready for httpx.

    Returns MozillaCookieJar (compatible with httpx.Client(cookies=jar)).
    Tolerates scrapmf-source lines, spaces vs TABs, #HttpOnly_, empty lines.
    This avoids httpx.Cookies CookieConflict when the same name exists on different domains
    (e.g. sessionid on tiktok + instagram).
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"cookies file not found: {p}")

    normalized = _normalize_netscape_file(p)
    try:
        jar = http.cookiejar.MozillaCookieJar(str(normalized))
        jar.load(ignore_discard=ignore_discard, ignore_expires=ignore_expires)
    finally:
        try:
            normalized.unlink()
        except Exception:
            pass
    return jar


def jar_to_httpx_cookies(jar: http.cookiejar.CookieJar) -> httpx.Cookies:
    """Convert CookieJar to httpx.Cookies (only for cases without name conflicts)."""
    cookies = httpx.Cookies()
    for c in jar:
        try:
            cookies.set(c.name, c.value, domain=c.domain, path=c.path)
        except Exception:
            # on conflict, use plain dict and continue
            pass
    return cookies


def load_cookies_dict(
    cookies: dict[str, str], *, domain: str = "threads.net"
) -> http.cookiejar.CookieJar:
    """Helper for tests / direct injection dict -> CookieJar."""
    jar = http.cookiejar.CookieJar()
    for k, v in cookies.items():
        c = http.cookiejar.Cookie(
            version=0,
            name=k,
            value=v,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=False,
            domain_initial_dot=False,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=False,
            comment=None,
            comment_url=None,
            rest={},
        )
        jar.set_cookie(c)
    return jar


def get_csrf_token(cookies: http.cookiejar.CookieJar | httpx.Cookies) -> str | None:
    """Extract csrftoken from cookies (required for x-csrftoken header in Threads GraphQL)."""
    try:
        # CookieJar iter
        for c in cookies:  # type: ignore[union-attr]
            if getattr(c, "name", None) == "csrftoken":
                return getattr(c, "value", None)
    except Exception:
        pass
    try:
        # httpx.Cookies fallback
        return cookies.get("csrftoken")  # type: ignore[no-any-return,attr-defined]
    except Exception:
        return None


def cookies_to_header_value(cookies: http.cookiejar.CookieJar | httpx.Cookies) -> str:
    """Serialize cookies to a Cookie: ... header (useful for curl_cffi)."""
    try:
        return "; ".join(f"{c.name}={c.value}" for c in cookies)  # type: ignore[union-attr]
    except Exception:
        return "; ".join(f"{k}={v}" for k, v in cookies.items())  # type: ignore[union-attr]


# Compat helper: save a jar to Netscape format for debugging
def save_netscape_cookies(
    cookies: http.cookiejar.CookieJar | httpx.Cookies, path: str | Path
) -> None:
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        items = list(cookies)  # type: ignore[union-attr]
        # if CookieJar, items are Cookie; if httpx.Cookies, iter yields (k,v) -> fallback
        if items and hasattr(items[0], "name"):
            for c in items:  # type: ignore
                jar.set_cookie(c)
        else:
            for k, v in cookies.items():  # type: ignore[union-attr]
                c = http.cookiejar.Cookie(
                    version=0,
                    name=k,
                    value=v,
                    port=None,
                    port_specified=False,
                    domain=".threads.net",
                    domain_specified=True,
                    domain_initial_dot=True,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                )
                jar.set_cookie(c)
    except Exception:
        pass
    jar.save(ignore_discard=True, ignore_expires=True)


def load_from_browser(browser: str, *, domain: str = "threads.com") -> http.cookiejar.CookieJar:
    """Load cookies directly from the browser (brave/chrome/firefox)
    like gallery-dl --cookies-from-browser.

    Requires: pip install browser-cookie3
    On Linux copies the DB to /tmp to avoid database locked while the browser is open.
    """
    try:
        import browser_cookie3  # type: ignore
    except ImportError as e:
        raise ImportError(
            "browser-cookie3 not installed. "
            "pip install 'threadstractormf[browser]' or 'browser-cookie3'"
        ) from e

    b = browser.lower()
    if b in ("brave", "brave-browser"):
        jar = browser_cookie3.brave(domain_name=domain)
    elif b == "chrome":
        jar = browser_cookie3.chrome(domain_name=domain)
    elif b == "firefox":
        jar = browser_cookie3.firefox(domain_name=domain)
    elif b == "edge":
        jar = browser_cookie3.edge(domain_name=domain)
    else:
        raise ValueError(f"unsupported browser: {browser} (use brave|chrome|firefox|edge)")

    # browser_cookie3 returns a CookieJar already filtered by domain
    # For threads we need both threads.com and threads.net plus instagram fallback
    if domain in ("threads.com", "threads.net"):
        try:
            extra = None
            if domain == "threads.com":
                extra = (
                    browser_cookie3.brave(domain_name="threads.net")
                    if b in ("brave", "brave-browser")
                    else None
                )
            if extra:
                for c in extra:
                    jar.set_cookie(c)
        except Exception:
            pass
    return jar


def ensure_netscape_header(path: str | Path) -> None:
    """Validate that the file has the MAGIC header; helper for clear error messages."""
    p = Path(path)
    if not p.exists():
        return
    first = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
    if not first or "HTTP Cookie File" not in first[0]:
        raise http.cookiejar.LoadError(
            f"{p} does not look like a Netscape format cookies file. "
            "Export with 'Get cookies.txt LOCALLY' (Netscape), not JSON. "
            "First line must be '# Netscape HTTP Cookie File' with TABs."
        )
