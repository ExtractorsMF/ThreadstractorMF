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
import time
from pathlib import Path
from typing import Any

import httpx


def _parse_netscape_lines(path: Path) -> list[str]:
    """Normalise a Netscape cookie file to TAB-separated lines, **in memory**.

    Tolerates the things real exports contain:
    - a ``scrapmf-source`` line above the MAGIC header
    - spaces instead of TABs (many generators emit spaces)
    - ``#HttpOnly_`` prefixes, which are stripped here and re-applied by
      :func:`load_netscape_cookies`
    - empty and comment lines, and malformed rows

    Returns the normalised lines; nothing is written to disk. This used to build
    a temp file purely because ``MozillaCookieJar.load()`` demands a filename,
    which put live session cookies on disk as ``0o644``.
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
    # Rebuild from MAGIC onwards. The header itself is NOT returned: it carries
    # no cookie, only MozillaCookieJar needed it to validate the file, and
    # ensure_netscape_header() already does that.
    out_lines: list[str] = []
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
        # Keep the #HttpOnly_ prefix: it is a real cookie attribute and the
        # consumer re-applies it as rest={"HttpOnly": ""}.
        httponly = raw_line.startswith("#HttpOnly_")
        line = raw_line[len("#HttpOnly_") :] if httponly else raw_line
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
        # Rebuild with TABs, re-attaching the prefix so the HttpOnly flag is
        # not lost.
        rebuilt = "\t".join([domain, flag, cpath, secure, expires, name, value])
        out_lines.append(f"#HttpOnly_{rebuilt}" if httponly else rebuilt)
    return out_lines


def load_netscape_cookies(
    path: str | Path, *, ignore_expires: bool = True, ignore_discard: bool = True
) -> http.cookiejar.CookieJar:
    """Load a Netscape cookies.txt and return a CookieJar ready for httpx.

    Returns a CookieJar (compatible with ``httpx.Client(cookies=jar)``).
    Tolerates scrapmf-source lines, spaces vs TABs, ``#HttpOnly_``, empty lines,
    and rows whose include-subdomains flag disagrees with the domain's leading
    dot. A CookieJar rather than ``httpx.Cookies`` avoids CookieConflict when the
    same name exists on different domains (e.g. sessionid on tiktok +
    instagram).

    The file is parsed in memory. It used to be rewritten into a
    ``tempfile.mktemp()`` file because ``MozillaCookieJar.load()`` requires a
    filename, which left live session cookies on disk as world-readable
    ``0o644`` and exposed a symlink race before the write.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"cookies file not found: {p}")

    # Surface the actionable message first: exporting cookies as JSON is by far
    # the most common mistake, and the bare LoadError does not hint at it.
    ensure_netscape_header(p)

    jar = http.cookiejar.CookieJar()
    now = time.time()
    for raw in _parse_netscape_lines(p):
        httponly = raw.startswith("#HttpOnly_")
        if httponly:
            raw = raw[len("#HttpOnly_") :]
        domain, flag, cpath, secure, expires, name, value = raw.split("\t")

        initial_dot = domain.startswith(".")
        # MozillaCookieJar asserts domain_specified == initial_dot, so a file
        # whose flag disagrees with its own domain aborted the entire load with
        # an AssertionError. Derive the flag from the domain instead, which is
        # what the Netscape format actually means.
        domain_specified = flag == "TRUE" or initial_dot

        # expires == "" and expires == "0" both mean "session cookie". Reading
        # "0" as a timestamp would date it to 1970 and drop a live cookie.
        try:
            expires_at = int(expires) if expires not in ("", "0") else None
        except ValueError:
            expires_at = None
        discard = expires_at is None
        if ignore_discard:
            discard = False
        if not ignore_expires and expires_at is not None and expires_at < now:
            continue

        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=domain_specified,
                domain_initial_dot=initial_dot,
                path=cpath,
                # MozillaCookieJar assumes path_specified is false.
                path_specified=False,
                secure=secure == "TRUE",
                expires=expires_at,
                discard=discard,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": ""} if httponly else {},
            )
        )
    return jar


def load_cookies_dict(
    cookies: dict[str, str], *, domain: str = ".threads.net"
) -> http.cookiejar.CookieJar:
    """Build a CookieJar from a plain ``{name: value}`` mapping.

    Used by ``Threadscraper`` so every accepted ``cookies`` input ends up in the
    same shape, which the Playwright cookie converter requires.
    """
    jar = http.cookiejar.CookieJar()
    for k, v in cookies.items():
        c = http.cookiejar.Cookie(
            version=0,
            name=k,
            value=v,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith("."),
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
    if isinstance(cookies, httpx.Cookies):
        return cookies.get("csrftoken")
    for c in cookies:
        if getattr(c, "name", None) == "csrftoken":
            value = getattr(c, "value", None)
            return str(value) if value is not None else None
    return None
def _load_brave_profile(profile_dir: str, domain: str, browser_cookie3: Any) -> Any:
    """Read cookies from a specific Brave profile (Origin, Beta, Nightly).

    ``browser_cookie3.brave()`` only resolves the default ``Brave-Browser``
    profile, so a live session kept in ``Brave-Origin`` is invisible to it. The
    ``Brave`` class takes explicit paths, which is what this uses; the
    ``Local State`` file next to it holds the encryption key.
    """
    import browser_cookie3 as _bc3  # noqa: F401  (resolved by caller)

    root = Path.home() / ".config" / "BraveSoftware" / profile_dir
    cookie_file = root / "Default" / "Cookies"
    key_file = root / "Local State"
    if not cookie_file.exists():
        raise FileNotFoundError(
            f"no Brave profile at {cookie_file}. "
            f"Is '{profile_dir}' installed, or is this a non-Linux layout?"
        )
    return browser_cookie3.Brave(
        str(cookie_file), domain_name=domain, key_file=str(key_file)
    ).load()


def _brave_variant(profile_dir: str, browser_cookie3: Any) -> Any:
    """Build a loader bound to one Brave profile directory."""
    return lambda domain: _load_brave_profile(profile_dir, domain, browser_cookie3)


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

    # Brave ships several independent profiles under the same config root, and
    # browser_cookie3 only has a shortcut for the default one:
    #
    #   browser_cookie3.brave()  ->  BraveSoftware/Brave-Browser   (the *stale* one)
    #
    # A user whose live session lives in Brave-Origin therefore gets an old jar
    # without any sign of it. The class accepts explicit paths, so each variant
    # is pointed at its own profile directory.
    loaders = {
        "brave": lambda d: browser_cookie3.brave(domain_name=d),
        "brave-browser": lambda d: browser_cookie3.brave(domain_name=d),
        "brave-origin": _brave_variant("Brave-Origin", browser_cookie3),
        "brave-nightly": _brave_variant("Brave-Browser-Nightly", browser_cookie3),
        "brave-beta": _brave_variant("Brave-Beta", browser_cookie3),
        "chrome": lambda d: browser_cookie3.chrome(domain_name=d),
        "chromium": lambda d: browser_cookie3.chromium(domain_name=d),
        "firefox": lambda d: browser_cookie3.firefox(domain_name=d),
        "edge": lambda d: browser_cookie3.edge(domain_name=d),
        "opera": lambda d: browser_cookie3.opera(domain_name=d),
        "vivaldi": lambda d: browser_cookie3.vivaldi(domain_name=d),
    }
    loader = loaders.get(b)
    if loader is None:
        raise ValueError(
            f"unsupported browser: {browser} (use {'|'.join(sorted(loaders))})"
        )
    jar: http.cookiejar.CookieJar = loader(domain)

    # browser_cookie3 filters by the requested domain only, but Threads keeps
    # the session split between threads.com and threads.net. Merge both for
    # every browser — previously only Brave got the threads.net half, silently
    # leaving Chrome/Firefox/Edge with an incomplete session.
    if domain == "threads.com":
        try:
            for c in loader("threads.net"):
                jar.set_cookie(c)
        except Exception:
            pass
    return jar


def ensure_netscape_header(path: str | Path) -> None:
    """Validate that the file carries the Netscape MAGIC header.

    The header is searched for anywhere in the file, not just on the first line,
    matching :func:`_parse_netscape_lines`: some exporters (scrapmf among them)
    prepend a source comment before the real header. Checking only line 1 would
    reject files the parser is happy to read.
    """
    p = Path(path)
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not any("HTTP Cookie File" in line for line in lines):
        raise http.cookiejar.LoadError(
            f"{p} does not look like a Netscape format cookies file. "
            "Export with 'Get cookies.txt LOCALLY' (Netscape), not JSON. "
            "The file must contain a line '# Netscape HTTP Cookie File'."
        )
