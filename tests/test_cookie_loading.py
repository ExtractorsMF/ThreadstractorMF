"""Regression: loading cookies must not write credentials to disk.

The old implementation normalised ``cookies.txt`` into ``tempfile.mktemp()``
purely because ``MozillaCookieJar.load()`` insists on a filename. That temp
file held live session cookies with ``0o644`` permissions — world readable — and
``mktemp`` does not create the file, leaving a symlink window before the write.
``MozillaCookieJar`` also asserts that the include-subdomains flag agrees with
the domain's leading dot, so a self-inconsistent export aborted the whole load.

These tests used to compare against a copy of the previous implementation kept
in a gitignored file, which meant none of them ran in CI — a fresh checkout
skipped the module entirely. Worse, that comparison asserted what the *old* code
did rather than what this one must do, which is what let two of them fail on
Python 3.14 for reasons unrelated to the loader. The behaviour is now pinned
directly.
"""

import http.cookiejar
import time
import urllib.request

import pytest

from threadstractormf.auth import (
    _parse_netscape_lines,
    ensure_netscape_header,
    load_netscape_cookies,
)

MAGIC = "# Netscape HTTP Cookie File"


def _write(tmp_path, body: str, name: str = "cookies.txt"):
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


def _cookies(jar) -> dict[str, http.cookiejar.Cookie]:
    return {c.name: c for c in jar}


# --- the parser, against the format it must accept ---------------------------

SAMPLE = (
    MAGIC + "\n"
    ".threads.net\tTRUE\t/\tTRUE\t2147483647\tsessionid\tV1\n"
    ".threads.com\tTRUE\t/\tFALSE\t0\tcsrftoken\tV2\n"
    "# a comment line\n"
    "\n"
    "#HttpOnly_.threads.net\tTRUE\t/\tTRUE\t0\thsid\tabc def\n"
    "malformed-line-without-tabs\n"
)


def test_a_realistic_export_loads_every_cookie_it_contains(tmp_path):
    """Every row of a normal export survives, with the right fields.

    This used to diff against a copy of the previous implementation, kept in a
    gitignored file so that CI skipped the whole module. The comparison was also
    the wrong shape: it asserted what the old code did, which is why two tests
    in this file failed on 3.14 over interpreter behaviour rather than ours.
    The contract is stated directly here instead.
    """
    f = _write(tmp_path, SAMPLE)
    jar = _cookies(load_netscape_cookies(f))

    assert set(jar) == {"sessionid", "csrftoken", "hsid"}, set(jar)

    session = jar["sessionid"]
    assert session.value == "V1"
    assert session.domain == ".threads.net"
    assert session.path == "/"
    assert session.secure is True
    # A real future expiry is kept verbatim.
    assert session.expires == 2147483647

    csrf = jar["csrftoken"]
    assert csrf.value == "V2"
    assert csrf.domain == ".threads.com"
    # Not secure: the export says FALSE, and upgrading it would send it to
    # plaintext when Threads itself does not.
    assert csrf.secure is False

    # ``#HttpOnly_`` rows load and keep the flag. It was stripped by the old
    # normaliser and never reapplied.
    httponly = jar["hsid"]
    assert httponly._rest.get("HttpOnly") == ""

    # A session cookie: expires 0 means "session", not "expired in 1970".
    assert httponly.expires is None
    assert not httponly.is_expired()

    # A line with no tabs at all is skipped, not fatal: real exports carry junk.
    assert "malformed-line-without-tabs" not in jar


def test_session_cookies_are_actually_sent(tmp_path):
    """A session cookie must actually reach the request.

    Netscape writes ``0`` for a session cookie. Storing that as the timestamp
    0 makes http.cookiejar read the cookie as expired and drop it, so it was
    loaded and then never sent. The contract is that ``0`` means "session", and
    the session id is what actually authenticates the API.

    Previously this also asserted that the old implementation dropped it, which
    stopped holding on 3.14: the interpreter no longer treats a stored
    ``expires=0`` as expired, so the "before" behaviour vanished without any
    change on our side. The requirement is now stated directly instead.
    """

    body = MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t0\tsessionid\tV1\n"
    f = _write(tmp_path, body)

    jar = load_netscape_cookies(f)
    request = urllib.request.Request("https://www.threads.net/graphql/query")
    jar.add_cookie_header(request)
    assert request.get_header("Cookie") == "sessionid=V1"

    # And it must not be treated as expired, which is the actual bug.
    (session,) = [c for c in jar if c.name == "sessionid"]
    assert session.expires is None
    assert not session.is_expired()


def test_space_separated_rows_load_too(tmp_path):
    """Not every export uses tabs; some use spaces.

    Reimplemented here rather than compared against the old loader: real
    exporters disagree on this, and the requirement is that both shapes work.
    """
    body = MAGIC + "\n.threads.net TRUE / TRUE 2147483647 sessionid V1\n"
    f = _write(tmp_path, body)
    jar = _cookies(load_netscape_cookies(f))

    assert set(jar) == {"sessionid"}
    assert jar["sessionid"].value == "V1"


def test_session_and_httponly_cookies_survive(tmp_path):
    f = _write(tmp_path, SAMPLE)
    jar = _cookies(load_netscape_cookies(f))
    assert set(jar) == {"sessionid", "csrftoken", "hsid"}
    assert jar["hsid"].value == "abc def", "a value containing spaces must survive"
    assert jar["hsid"]._rest.get("HttpOnly") == "", "the HttpOnly flag is preserved"


def test_leading_dot_is_kept_on_the_domain(tmp_path):
    """Playwright and httpx both scope by domain, so the dot must not be lost."""
    f = _write(tmp_path, MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t0\tsessionid\tV\n")
    cookie = _cookies(load_netscape_cookies(f))["sessionid"]
    assert cookie.domain == ".threads.net"
    assert cookie.domain_initial_dot is True
    assert cookie.domain_specified is True


def test_expired_cookies_are_kept_by_default(tmp_path):
    """Session cookies in the wild carry stale expiries; dropping them logs you out."""
    past = int(time.time()) - 86_400
    f = _write(tmp_path, MAGIC + f"\n.threads.net\tTRUE\t/\tTRUE\t{past}\tsessionid\tV\n")
    assert "sessionid" in _cookies(load_netscape_cookies(f))
    assert "sessionid" not in _cookies(load_netscape_cookies(f, ignore_expires=False))


def test_blank_expiry_is_a_session_cookie(tmp_path):
    f = _write(tmp_path, MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t\tsessionid\tV\n")
    cookie = _cookies(load_netscape_cookies(f))["sessionid"]
    assert cookie.expires is None
    assert cookie.discard is False, "ignore_discard defaults to True"


# --- the inconsistencies MozillaCookieJar used to abort on -----------------


def test_inconsistent_flag_does_not_abort_the_load(tmp_path, recwarn):
    """MozillaCookieJar asserts domain_specified == initial_dot, which turned a
    self-inconsistent export into a LoadError plus a stdlib warning."""
    body = (
        MAGIC
        + "\n.threads.net\tFALSE\t/\tTRUE\t0\tsessionid\tV\n"
        + ".other\tTRUE\t/\tTRUE\t0\tok\tW\n"
    )
    f = _write(tmp_path, body)
    jar = _cookies(load_netscape_cookies(f))
    assert set(jar) == {"sessionid", "ok"}, "one bad row must not lose the others"
    assert jar["sessionid"].domain_specified is True, "the dot implies subdomains"
    assert not [w for w in recwarn if issubclass(w.category, UserWarning)]


def test_host_only_cookie_is_not_marked_domain_specified(tmp_path):
    f = _write(tmp_path, MAGIC + "\nthreads.net\tFALSE\t/\tTRUE\t0\tsessionid\tV\n")
    cookie = _cookies(load_netscape_cookies(f))["sessionid"]
    assert cookie.domain == "threads.net"
    assert cookie.domain_specified is False
    assert cookie.domain_initial_dot is False


# --- nothing lands on disk -------------------------------------------------


def test_no_file_is_created_in_tmp(tmp_path, monkeypatch):
    """Regression: session cookies used to be written to /tmp as 0o644."""
    created: list[str] = []
    import tempfile as tempfile_mod

    real_mkstemp = tempfile_mod.mkstemp
    real_mkdtemp = tempfile_mod.mkdtemp
    real_named = tempfile_mod.NamedTemporaryFile

    def spy_mkstemp(*a, **k):
        created.append("mkstemp")
        return real_mkstemp(*a, **k)

    def spy_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created.append(f"mkdtemp:{path}")
        return path

    def spy_named(*a, **k):
        created.append("NamedTemporaryFile")
        return real_named(*a, **k)

    monkeypatch.setattr(tempfile_mod, "mkstemp", spy_mkstemp)
    monkeypatch.setattr(tempfile_mod, "mkdtemp", spy_mkdtemp)
    monkeypatch.setattr(tempfile_mod, "NamedTemporaryFile", spy_named)
    monkeypatch.setattr(tempfile_mod, "mktemp", lambda *a, **k: created.append("mktemp"))

    f = _write(tmp_path, SAMPLE)
    jar = load_netscape_cookies(f)
    assert len(_cookies(jar)) == 3
    assert created == [], f"no temp file may be created, got {created}"


def test_no_leftover_cookies_file_in_tmp(tmp_path):
    import glob
    import tempfile as tempfile_mod

    pattern = f"{tempfile_mod.gettempdir()}/*.cookies.txt"
    before = set(glob.glob(pattern))
    f = _write(tmp_path, SAMPLE)
    load_netscape_cookies(f)
    assert set(glob.glob(pattern)) == before


# --- the parser itself -----------------------------------------------------


def test_parse_returns_lines_without_writing(tmp_path):
    f = _write(tmp_path, SAMPLE)
    lines = _parse_netscape_lines(f)
    assert isinstance(lines, list)
    assert lines, "the sample has cookies"
    assert all(isinstance(line, str) for line in lines)
    assert all("\t" in line for line in lines), "every returned line is a cookie row"
    assert all(not line.startswith("# ") for line in lines), "no header or comments"
    # HttpOnly rows keep their prefix so the flag can be reapplied.
    assert any(line.startswith("#HttpOnly_") for line in lines)


def test_spaces_are_converted_to_tabs(tmp_path):
    f = _write(tmp_path, MAGIC + "\n.threads.net TRUE / TRUE 0 sessionid V\n")
    assert _parse_netscape_lines(f)[0] == ".threads.net\tTRUE\t/\tTRUE\t0\tsessionid\tV"


def test_header_not_first_is_tolerated(tmp_path):
    """scrapmf prepends a source line; the real MAGIC can start lower down."""
    body = "# scrapmf-source: something\n" + MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t0\tsid\tV\n"
    f = _write(tmp_path, body)
    jar = _cookies(load_netscape_cookies(f))
    assert set(jar) == {"sid"}


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    body = MAGIC + "\nno-tabs-here\n.threads.net\tTRUE\t/\tTRUE\t0\tsid\tV\nshort\tline\n"
    f = _write(tmp_path, body)
    assert set(_cookies(load_netscape_cookies(f))) == {"sid"}


def test_missing_magic_raises(tmp_path):
    f = _write(tmp_path, ".threads.net\tTRUE\t/\tTRUE\t0\tsid\tV\n")
    with pytest.raises(http.cookiejar.LoadError):
        load_netscape_cookies(f)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_netscape_cookies(tmp_path / "nope.txt")


def test_json_export_gets_the_actionable_message(tmp_path):
    f = _write(tmp_path, '[{"name":"sessionid","value":"x"}]', "cookies.json")
    with pytest.raises(http.cookiejar.LoadError, match="not JSON"):
        load_netscape_cookies(f)
    with pytest.raises(http.cookiejar.LoadError, match="not JSON"):
        ensure_netscape_header(f)


def test_plain_load_still_works(tmp_path):
    f = _write(tmp_path, MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\tabc\n")
    jar = _cookies(load_netscape_cookies(f))
    assert jar["csrftoken"].value == "abc"
    assert jar["csrftoken"].secure is True
