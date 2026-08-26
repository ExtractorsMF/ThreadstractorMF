import http.cookiejar
from pathlib import Path

from threadstractormf.auth import get_csrf_token, load_netscape_cookies


def test_load_netscape_cookies(tmp_path: Path):
    # Create minimal Netscape cookies.txt (gallery-dl compatible) with TABs
    txt = tmp_path / "cookies.txt"
    txt.write_text(
        "# Netscape HTTP Cookie File\n"
        ".threads.net\tTRUE\t/\tTRUE\t2147483647\tcsrftoken\tabc123\n"
        ".threads.net\tTRUE\t/\tFALSE\t2147483647\tsessionid\txyz\n"
    )
    jar = load_netscape_cookies(txt)
    # jar es CookieJar
    vals = {c.name: c.value for c in jar}
    assert vals["csrftoken"] == "abc123"
    assert get_csrf_token(jar) == "abc123"


def test_missing_header_raises(tmp_path: Path):
    txt = tmp_path / "bad.txt"
    txt.write_text("csrftoken=abc\n")
    try:
        load_netscape_cookies(txt)
    except http.cookiejar.LoadError:
        pass
    else:
        raise AssertionError("must fail without MAGIC header")
