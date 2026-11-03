"""Discover the current GraphQL request parameters from the live web app.

Why this module exists
----------------------
The Threads profile-media query is a *persisted query*: the client sends no
query text, only ``fb_api_req_friendly_name`` plus the ``doc_id`` that indexes
the server-side copy. When Meta edits the query it registers a **new** doc_id
and the old one stops resolving — the server answers

    {"errors": [{"message": "execution error", "severity": "CRITICAL"}]}

which says nothing about what went wrong. That is exactly what a hardcoded
constant cannot survive: v1.2.0 shipped ``doc_id 37598244946487292`` and it
went stale, so every scrape silently fell through to the slow DOM fallback.

The id is a server-side index. It cannot be derived, guessed, or computed — the
only source of truth is the request the web app itself issues. So we watch that
request once, keep it, and replay it.

Measured, not assumed
---------------------
Comparing the page-1 request against page-3 of the same scroll (2026-10):

* identical across pages: ``doc_id``, ``fb_dtsg``, ``lsd``, ``jazoest``,
  ``__rev``, ``__s``, ``__hsi``, ``av``, ``__crn``, ``__spin_*``, ``__user``
* different per request: ``__csr``, ``__dyn``, ``__hsdp``, ``__sjsp``,
  ``__req`` (a counter) and ``variables``

Replaying with only the stable set — the per-request nonces dropped, cursor
moved to page 2 — returned 10 edges and 0 errors. So a captured template is
reusable for every page of the walk.

The nonces are still stored. They are anti-abuse tokens rather than part of the
query, and keeping them costs nothing; if a future deploy starts demanding them
per request, re-using the captured values is the first thing to try.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs

# The query we need a template for. Stable across doc_id rotations: Meta reuses
# the friendly name and issues a new id.
MEDIA_QUERY_NAME = "BarcelonaProfileMediaTabRefetchableDirectQuery"

_GRAPHQL_PATH = "https://www.threads.com/graphql/query"

# Where the capture lives. XDG, with the documented fallbacks, so the template
# survives across CLI invocations — the whole point is to pay for the browser
# once, not once per process.
_CACHE_SUBDIR = "threadstractormf"
_CACHE_FILENAME = "request_template.json"

# How long an unused template is kept. The tokens in it are session-bound and
# therefore dead long before this, so it is a grace period for convenience,
# not for correctness: recapturing costs one browser launch.
_CACHE_MAX_AGE_DAYS = 14

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Only these are replayed. Verified stable across pages; see the module
# docstring. Anything else the browser sends is a per-request nonce.
STABLE_FIELDS = (
    "doc_id",
    "fb_api_req_friendly_name",
    "fb_api_caller_class",
    "fb_dtsg",
    "lsd",
    "jazoest",
    "av",
    "__user",
    "__a",
    "__rev",
    "__s",
    "__hsi",
    "__hs",
    "__crn",
    "__spin_r",
    "__spin_b",
    "__spin_t",
    "dpr",
    "__ccg",
    "server_timestamps",
)

# Kept but not replayed by default: re-used as a base if the stable set ever
# stops being enough.
NONCE_FIELDS = ("__csr", "__dyn", "__hsdp", "__sjsp", "__hblp", "__comet_req")


def session_fingerprint(cookies: Any) -> str:
    """A stable key for *which account* a captured template belongs to.

    Why this exists: the captured request carries session-bound tokens — ``lsd``,
    ``fb_dtsg``, ``__s``, ``__hsi``, and ``av`` (the caller's own id). Replaying
    them under a different account's cookies gets the query refused, and each
    refusal rediscovered and overwrote the file, so two users sharing a machine
    traded the cache back and forth and every scrape paid the browser.

    Keyed by a hash of the session cookie rather than the raw value: the cache
    file is user-readable, and a sessionid is a live credential.
    """
    values: list[str] = []
    for cookie in cookies or ():
        name = getattr(cookie, "name", None)
        if name in {"sessionid", "ds_user_id"}:
            values.append(f"{name}={getattr(cookie, 'value', '')}")
    if not values:
        # Anonymous: the tokens are not tied to an account, so one shared
        # bucket is correct and still avoids a recapture per profile.
        return "anonymous"
    digest = hashlib.sha256("\n".join(sorted(values)).encode()).hexdigest()[:16]
    return digest


def _cache_path(fingerprint: str = "anonymous", username: str = "") -> Path:
    """XDG cache location for the captured template.

    One file per (session, account). Both parts matter: the tokens are bound to
    the session, and the captured ``variables.userID`` — the account the browser
    actually queried — belongs to the profile that was visited. A template
    captured for @a is meaningless for @b.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        root = Path(base)
    else:
        home = Path.home()
        root = home / ".cache" if os.name == "posix" else home / "AppData" / "Local"
    suffix = f".{fingerprint}"
    if username:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", username)[:40]
        suffix = f".{safe}{suffix}"
    return root / _CACHE_SUBDIR / f"{_CACHE_FILENAME}{suffix}"


def legacy_cache_path() -> Path:
    """The pre-1.3 single-file cache, kept so it can be cleaned up or ignored."""
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        root = Path(base)
    else:
        home = Path.home()
        root = home / ".cache" if os.name == "posix" else home / "AppData" / "Local"
    return root / _CACHE_SUBDIR / _CACHE_FILENAME


@dataclass
class RequestTemplate:
    """A replayable Threads GraphQL request body.

    ``params`` holds the query's own fields; ``nonces`` the per-request
    anti-abuse tokens, kept for the day they become mandatory.

    ``variables`` is the captured ``variables`` blob, and it is **not**
    optional decoration. The query carries ~38
    ``__relay_internal__pv__Barcelona*relayprovider`` feature flags, and Threads
    refuses the query with ``execution error`` when they are missing — measured,
    not assumed: the same captured body answered with 12 edges when they were
    replayed and 0 edges from the 7-key set this module used to send. So the
    flags travel with the template and only the fields we actually control
    (``userID``, the cursor, the page size) are overwritten per request.
    """

    params: dict[str, str] = field(default_factory=dict)
    nonces: dict[str, str] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    username: str = ""
    user_id: str = ""
    endpoint: str = _GRAPHQL_PATH
    captured_at: float = 0.0

    @property
    def doc_id(self) -> str:
        return self.params.get("doc_id", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "params": self.params,
            "nonces": self.nonces,
            "variables": self.variables,
            "username": self.username,
            "user_id": self.user_id,
            "endpoint": self.endpoint,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestTemplate:
        variables = data.get("variables")
        return cls(
            params={str(k): str(v) for k, v in (data.get("params") or {}).items()},
            nonces={str(k): str(v) for k, v in (data.get("nonces") or {}).items()},
            variables=variables if isinstance(variables, dict) else {},
            username=str(data.get("username") or ""),
            user_id=str(data.get("user_id") or ""),
            endpoint=str(data.get("endpoint") or _GRAPHQL_PATH),
            captured_at=float(data.get("captured_at") or 0.0),
        )

    def is_usable(self) -> bool:
        """A template is replayable only with an id, an lsd, and its variables.

        The variables are part of the requirement, not a nicety: a template
        without the relay flags replays into ``execution error`` every time, so
        loading one without them would produce a template that looks fine and
        never works.
        """
        return bool(
            self.params.get("doc_id")
            and self.params.get("lsd")
            and self.variables
        )


def load_template(fingerprint: str = "anonymous", username: str = "") -> RequestTemplate | None:
    """Read the cached template for this session, or None when absent/corrupt.

    A cache that cannot be parsed is treated as absent rather than fatal: the
    caller's response to a missing template is to capture a new one, and that
    path must not itself raise.
    """
    path = _cache_path(fingerprint, username)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    template = RequestTemplate.from_dict(data)
    return template if template.is_usable() else None


def save_template(
    template: RequestTemplate, fingerprint: str = "anonymous", username: str = ""
) -> None:
    """Persist the template so the next run skips the browser.

    Best effort: a read-only or full home must not break a scrape that is
    otherwise working in memory.

    Written to a temporary file and renamed, so a crash or a second process
    reading mid-write cannot leave truncated JSON behind. Parallel scrapes on
    one machine are normal for scrapmf, and a corrupt cache would send every
    one of them back to the browser.

    Old templates are pruned: one file per (session, account), so every
    logout/login and every new profile adds a file, and nothing was ever
    removing them. The tokens in them are dead by then, so they are pure
    clutter. Only files untouched for ``_CACHE_MAX_AGE_DAYS`` go, and the one
    just written is excluded from the sweep.
    """
    path = _cache_path(fingerprint, username or template.username)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        # Crear el temporal con modo 0600 desde el inicio, evitando la ventana
        # en la que existiría con la umask por defecto.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(template.to_dict(), indent=2))
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except OSError:
        return
    _prune_old_templates(path)


def _prune_old_templates(keep: Path) -> None:
    """Delete cached templates nobody has looked at in a while.

    Best effort and deliberately conservative: an error here must never matter
    to a scrape that already has its template, and a file is only removed once
    it is clearly stale. Anything that is not a plain file is left alone.
    """
    cutoff = time.time() - _CACHE_MAX_AGE_DAYS * 86400
    try:
        entries = list(keep.parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry == keep:
            continue
        try:
            if not entry.is_file():
                continue
            # Solo se podan archivos de caché de esta herramienta; cualquier
            # otro archivo del directorio se preserva.
            if not entry.name.startswith("request_template.json"):
                continue
            if entry.stat().st_mtime > cutoff:
                continue
            entry.unlink()
        except OSError:
            continue


def clear_template(fingerprint: str = "anonymous", username: str = "") -> None:
    """Drop the cached template after the server rejected it."""
    try:
        _cache_path(fingerprint, username).unlink()
    except OSError:
        pass


def build_body(template: RequestTemplate, variables: dict[str, Any]) -> dict[str, str]:
    """The form body for one page: captured params + the merged variables.

    ``variables`` overrides the captured blob rather than replacing it. Threads
    validates the whole ``variables`` object against the registered query —
    dropping the ``__relay_internal__pv__Barcelona*`` feature flags that came
    with it turns a working request into ``execution error`` — so the capture
    is the baseline and only the keys passed in here are changed (the target
    account, the cursor, the page size).
    """
    body = dict(template.params)
    merged = dict(template.variables)
    merged.update(variables)
    body["variables"] = json.dumps(merged)
    return body


def _extract_user_id(body: dict[str, str]) -> str:
    """Pull the target account's id out of the captured variables."""
    try:
        parsed = json.loads(body.get("variables") or "{}")
    except ValueError:
        return ""
    uid = parsed.get("userID")
    return str(uid) if uid else ""


def _parse_variables(raw: str | None) -> dict[str, Any]:
    """Decode the captured ``variables`` blob.

    Kept whole, relay flags included — see :class:`RequestTemplate`. A blob that
    will not decode yields an empty dict, which makes the template unusable
    rather than replayable-but-wrong.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def discover(username: str, cookies: Any) -> RequestTemplate:
    """Capture a replayable template by watching the web app's own request.

    Raises RuntimeError when no matching request was observed — the caller
    decides whether that is fatal or just a reason to fall back.
    """
    from playwright.sync_api import sync_playwright

    from threadstractormf._backend import chromium_launch_args, to_playwright_cookies

    captured: list[dict[str, str]] = []
    url = f"https://www.threads.com/@{username}/media"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=chromium_launch_args(),
        )
        try:
            context = browser.new_context(user_agent=_UA)
            pw = to_playwright_cookies(cookies)
            if pw:
                # Best effort: an anonymous capture is still useful, it just
                # may not carry a session-bound lsd.
                try:
                    context.add_cookies(cast("Any", pw))
                except Exception:
                    pass
            page = context.new_page()

            def on_request(request: Any) -> None:
                try:
                    post_data = request.post_data or ""
                except Exception:
                    return
                if MEDIA_QUERY_NAME not in post_data:
                    return
                if "graphql" not in request.url.lower():
                    return
                captured.append({k: v[0] for k, v in parse_qs(post_data).items()})

            page.on("request", on_request)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # The profile tab fires its own query on load; the wait is generous
            # because a slow first paint must not read as "no query happened".
            page.wait_for_timeout(8000)
            # One scroll in case the first page was served from cache and the
            # query only fires on the next batch.
            try:
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(3000)
            except Exception:
                pass
        finally:
            browser.close()

    for body in captured:
        if body.get("doc_id") and body.get("lsd"):
            template = RequestTemplate(
                params={k: body[k] for k in STABLE_FIELDS if k in body},
                nonces={k: body[k] for k in NONCE_FIELDS if k in body},
                variables=_parse_variables(body.get("variables")),
                username=username.lstrip("@"),
                user_id=_extract_user_id(body),
                endpoint=_GRAPHQL_PATH,
                captured_at=time.time(),
            )
            if template.is_usable():
                return template

    raise RuntimeError(
        f"no {MEDIA_QUERY_NAME} request observed while loading {url}; "
        "Threads may have changed its client, or the profile is unavailable"
    )


def discover_and_cache(username: str, cookies: Any) -> RequestTemplate:
    """Capture a template and persist it for subsequent runs."""
    template = discover(username, cookies)
    save_template(template, session_fingerprint(cookies), template.username)
    return template
