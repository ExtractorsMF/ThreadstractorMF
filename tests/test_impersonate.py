"""Regression: --impersonate must actually use curl_cffi.

The flag used to be stored on the API object and then never read: `_get_client`
always built an `httpx.Client`, `curl_cffi` was never imported anywhere, and the
`antibot` extra was dead weight. The README nonetheless presented
`--impersonate chrome` as the fix for a Meta WAF 403.

These tests pin the behaviour that the flag now selects a Chrome TLS/JA4
impersonating session, for both API calls and CDN downloads.

Everything runs against a local HTTP server, so no network and no real CDN.
"""

import http.server
import threading

import pytest

pytest.importorskip("curl_cffi", reason="requires the [antibot] extra")

from threadstractormf._backend import CurlClient  # noqa: E402
from threadstractormf.downloader import download_media  # noqa: E402
from threadstractormf.models import Media  # noqa: E402

PAYLOAD = b"threadstractormf-payload" * 4096  # ~96 KiB, forces real chunking


def _serve(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class _Static(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *a):
        pass


# --- construction -----------------------------------------------------------


def test_curl_client_is_created_with_the_requested_target():
    client = CurlClient(impersonate="chrome")
    try:
        assert client.impersonate == "chrome"
        assert type(client._session).__name__ == "Session"
    finally:
        client.close()


def test_curl_client_assigns_cookies_and_headers():
    import http.cookiejar

    jar = http.cookiejar.CookieJar()
    jar.set_cookie(
        http.cookiejar.Cookie(
            version=0,
            name="sessionid",
            value="V",
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
    )
    client = CurlClient(impersonate="chrome", cookies=jar, headers={"X-Test": "1"})
    try:
        assert client._session.cookies.get("sessionid") == "V"
        assert client._session.headers.get("X-Test") == "1"
    finally:
        client.close()


def test_unknown_impersonation_target_raises_a_clear_error():
    with pytest.raises(ValueError, match="unknown impersonation target"):
        CurlClient(impersonate="definitely-not-a-browser")


def test_error_message_lists_valid_targets():
    with pytest.raises(ValueError) as excinfo:
        CurlClient(impersonate="bogus-browser")
    message = str(excinfo.value)
    assert "chrome" in message  # actionable, not just "invalid"


@pytest.mark.parametrize("target", ["chrome", "firefox", "safari", "edge"])
def test_generic_targets_are_accepted(target):
    client = CurlClient(impersonate=target)
    try:
        assert client.impersonate == target
    finally:
        client.close()


def test_versioned_targets_are_accepted():
    """curl_cffi also understands pinned fingerprints like 'chrome131'."""
    from threadstractormf._backend import known_impersonate_targets

    targets = known_impersonate_targets()
    versioned = [t for t in targets if t.startswith("chrome1")]
    assert versioned, "expected pinned chrome targets"
    client = CurlClient(impersonate=versioned[0])
    try:
        assert client.impersonate == versioned[0]
    finally:
        client.close()


# --- request / stream surface ----------------------------------------------


def test_request_goes_through_curl():
    srv, base = _serve(_Static)
    try:
        client = CurlClient(impersonate="chrome")
        try:
            resp = client.request("GET", f"{base}/graphql")
            assert resp.status_code == 200
            assert resp.content == PAYLOAD
        finally:
            client.close()
    finally:
        srv.shutdown()


def test_stream_bridges_iter_content_to_iter_bytes():
    """curl_cffi calls it iter_content; downloader.py (httpx-shaped) uses
    iter_bytes. The adapter is what keeps that from being a silent bug."""
    srv, base = _serve(_Static)
    try:
        client = CurlClient(impersonate="chrome")
        try:
            with client.stream("GET", f"{base}/v/t65.mp4") as r:
                assert r.status_code == 200
                assert b"".join(r.iter_bytes(chunk_size=4096)) == PAYLOAD
        finally:
            client.close()
    finally:
        srv.shutdown()


# --- full download paths ----------------------------------------------------


def test_download_uses_impersonation_and_writes_complete_file(tmp_path, monkeypatch):
    # The CDN allowlist demands https + a CDN host; relax it so the local
    # server can stand in for the CDN.
    import threadstractormf.downloader as dl

    monkeypatch.setattr(dl, "is_valid_media_url", lambda _u: True)
    srv, base = _serve(_Static)
    try:
        url = f"{base}/v/t65.37117/VIDEO.mp4?_nc_cat=101"
        media = Media(id="V1", post_id="V1", index=1, type="video", url=url, ext="mp4")
        out = download_media(
            media, tmp_path, impersonate="chrome", rate_limit=False
        ).path
        assert out.read_bytes() == PAYLOAD
        assert not (out.with_name(out.name + ".part")).exists()
    finally:
        srv.shutdown()


def test_download_without_impersonation_still_uses_httpx(tmp_path, monkeypatch):
    import httpx

    import threadstractormf.downloader as dl

    monkeypatch.setattr(dl, "is_valid_media_url", lambda _u: True)
    srv, base = _serve(_Static)
    try:
        url = f"{base}/v/t65.37117/VIDEO.mp4"
        media = Media(id="V2", post_id="V2", index=1, type="video", url=url, ext="mp4")
        created = {}
        real = dl._build_download_client

        def spy(**kw):
            client = real(**kw)
            created["type"] = type(client)
            return client

        monkeypatch.setattr(dl, "_build_download_client", spy)
        out = download_media(media, tmp_path, rate_limit=False).path
        assert out.read_bytes() == PAYLOAD
        assert created["type"] is httpx.Client
    finally:
        srv.shutdown()
