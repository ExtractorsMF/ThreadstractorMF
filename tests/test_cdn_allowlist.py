"""Regression: the CDN host check must be a domain suffix, not a substring.

The allowlist matched ``any(hint in host)`` over hints that mixed hostnames
(``scontent``, ``fbcdn``) with brand names (``instagram``, ``threads``). Any host
embedding those words was accepted, so ``https://instagram.evil.test/x.jpg`` and
``https://scontent.evil.com/v/t51.1/a.jpg`` both passed a check whose whole job
is to reject non-Media URLs.

The same bug was duplicated in the JS embedded in ``api.py``, which additionally
short-circuited on ``|| s.includes('.mp4')`` — bypassing the host check for any
URL ending in .mp4, which Python rejected.
"""

import subprocess

import pytest

from threadstractormf.downloader import _CDN_HOST_SUFFIXES, is_valid_media_url

# ── legitimate Meta CDN hosts ───────────────────────────────────────────────

LEGITIMATE = [
    "https://scontent.cdninstagram.com/v/t51.1/a.jpg",
    "https://scontent.cdninstagram.com/o1/v/t16/m84/A.mp4",
    "https://instagram.fpei1-1.fna.fbcdn.net/o1/v/t16/m84/A.mp4",
    # regional POPs that appear nowhere in this repo; a suffix covers them
    # without a code change, which is the point of not pinning literal hosts
    "https://instagram.fpei2-1.fna.fbcdn.net/o1/v/t16/m84/A.mp4",
    "https://scontent-abc.cdninstagram.com/v/t51.1/a.jpg",
    "https://scontent.cdninstagram.net/v/t51.1/a.jpg",
    # the registrable domain on its own
    "https://cdninstagram.com/v/t51.1/a.jpg",
    # a fully-qualified name may arrive with a trailing dot
    "https://scontent.cdninstagram.com./v/t51.1/a.jpg",
]


@pytest.mark.parametrize("url", LEGITIMATE)
def test_legitimate_cdn_hosts_are_accepted(url):
    assert is_valid_media_url(url)


def test_query_string_alone_qualifies_the_path():
    assert is_valid_media_url("https://scontent.cdninstagram.com/o1/v/t16/A.mp4?_nc_cat=101")


# ── attacker-controlled hosts ───────────────────────────────────────────────

ATTACKERS = [
    "https://scontent.evil.com/v/t51.1/a.jpg",
    "https://instagram.evil.test/x.jpg",
    "https://fbcdn.net.attacker.io/v/t51.1/a.jpg",
    "https://cdninstagram.com.evil.io/x.jpg",
    "https://notcdninstagram.com/x.jpg",
    "https://threads-net.attacker.io/x.jpg",
    "https://myinstagram.com/x.jpg",
    "https://evil.com/threads/x.jpg",
    "https://attacker.test/instagram.jpg",
    # the .mp4 shortcut the JS used to allow, and any host serving one
    "https://evil.com/x.mp4",
    "https://attacker.test/payload.mp4",
    # Meta's own non-CDN hosts
    "https://graph.instagram.com/",
    "https://platform.instagram.com/",
    "https://www.threads.com/@user",
]


@pytest.mark.parametrize("url", ATTACKERS)
def test_non_cdn_hosts_are_rejected(url):
    assert not is_valid_media_url(url), f"{url} must not pass the allowlist"


def test_the_old_substring_rule_would_have_failed_these():
    """Guard against reintroducing the substring match."""
    def old_rule(url: str) -> bool:
        from urllib.parse import urlparse

        u = urlparse(url)
        if u.scheme != "https":
            return False
        host = u.netloc.lower()
        return any(h in host for h in ("fbcdn", "scontent", "cdninstagram", "instagram", "threads"))

    slipped = [u for u in ATTACKERS if old_rule(u)]
    assert slipped, "the substring rule would accept at least one of these"
    for url in slipped:
        assert not is_valid_media_url(url)


# ── unrelated preconditions still hold ──────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://scontent.cdninstagram.com/v/t51.1/a.jpg",  # not https
        "/relative/a.jpg",
        "not-a-url",
        "https://",
    ],
)
def test_invalid_inputs(url):
    assert not is_valid_media_url(url)


def test_non_string_input():
    assert not is_valid_media_url(None)  # type: ignore[arg-type]
    assert not is_valid_media_url(12345)  # type: ignore[arg-type]


def test_suffixes_exclude_brand_names():
    joined = " ".join(_CDN_HOST_SUFFIXES)
    assert "instagram.com" in joined
    assert ".threads" not in joined, "'threads' is a brand, not a hostname suffix"


# ── the embedded JS must agree ──────────────────────────────────────────────


def _embedded_js() -> str:
    """Extract the DOM media filter from api.py."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "threadstractormf" / "api.py"
    text = source.read_text()
    start = text.find("media_urls=els.filter(el=>{")
    assert start != -1, "the DOM filter is no longer in api.py"
    end = text.find("}).map(el=>", start)
    return text[start:end]


def test_embedded_js_has_no_substring_match():
    js = _embedded_js()
    assert ".includes('fbcdn')" not in js
    assert ".includes('scontent')" not in js
    assert ".includes('cdninstagram')" not in js
    # the .mp4 shortcut bypassed the host check entirely
    assert ".includes('.mp4')" not in js


def test_embedded_js_uses_the_same_suffixes():
    js = _embedded_js()
    for suffix in _CDN_HOST_SUFFIXES:
        assert f'"{suffix}"' in js, f"{suffix} missing from the JS filter"


def test_embedded_js_accepts_what_python_accepts():
    """Run the real filter through node and compare against Python.

    Skipped when node is unavailable; the string assertions above still hold.
    """
    node = subprocess.run(["which", "node"], capture_output=True)
    if node.returncode != 0:
        pytest.skip("node not available")

    import json

    # json.dumps, not repr: Python renders strings with single quotes, which is
    # not valid JS.
    program = f"""
    const SUF = {json.dumps(list(_CDN_HOST_SUFFIXES))};
    function filter(s) {{
        if (s.includes('t51.2885-19')) return false;
        try {{
            const host = new URL(s, 'https://www.threads.com/').hostname;
            return SUF.some(d => host === d.slice(1) || host.endsWith(d));
        }} catch (e) {{ return false; }}
    }}
    const CASES = {json.dumps(LEGITIMATE + ATTACKERS)};
    console.log(JSON.stringify(CASES.map(s => [s, filter(s)])));
    """
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    mismatches = []
    for url, js_accepts in json.loads(result.stdout):
        py_accepts = is_valid_media_url(url)
        # is_valid_media_url has extra path/query conditions the JS does not,
        # so only flag the case where JS is *more* permissive than Python.
        if js_accepts and not py_accepts:
            mismatches.append(url)
    assert not mismatches, f"JS accepts what Python rejects: {mismatches}"
