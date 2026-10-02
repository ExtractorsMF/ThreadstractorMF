"""Regression: download retry policy for transient network errors.

- is_transient_error classifies DNS flaps / timeouts / CDN 5xx as retryable
  and 4xx or programming errors as permanent.
- download_media retries transient failures (with .part atomic writes) and
  never leaves a truncated media at the final path.
"""

from pathlib import Path

import httpx
import pytest

from threadstractormf._backend import is_transient_error
from threadstractormf.downloader import (
    _CDN_TIMEOUT,
    _DOWNLOAD_ATTEMPTS,
    _DOWNLOAD_BACKOFF_S,
    download_media,
)
from threadstractormf.models import Media


def _media() -> Media:
    return Media(
        id="T1",
        post_id="T1",
        index=1,
        type="video",
        url="https://scontent.cdninstagram.com/v/t65.37117/x.mp4?a=1",
        ext="mp4",
    )


def test_cdn_timeout_is_generous():
    # v1.0.2 fail-fast values (5/15) truncated real downloads on slow links
    assert _CDN_TIMEOUT.connect >= 15.0
    assert _CDN_TIMEOUT.read >= 45.0


def test_transient_dns_and_timeout_and_5xx_are_retryable():
    assert is_transient_error(httpx.ConnectError("[Errno -3] gaierror"))
    assert is_transient_error(httpx.ConnectTimeout("connect timeout"))
    assert is_transient_error(httpx.ReadTimeout("read timeout"))
    assert is_transient_error(httpx.RemoteProtocolError("peer closed"))
    resp500 = httpx.Response(500, request=httpx.Request("GET", "https://cdn/x.mp4"))
    assert is_transient_error(
        httpx.HTTPStatusError("boom", request=resp500.request, response=resp500)
    )


def test_permanent_4xx_and_other_errors_are_not_retryable():
    resp404 = httpx.Response(404, request=httpx.Request("GET", "https://cdn/x.mp4"))
    assert not is_transient_error(
        httpx.HTTPStatusError("nope", request=resp404.request, response=resp404)
    )
    assert not is_transient_error(ValueError("bad url"))


def test_download_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # backoff sleeps are patched out: we care about the loop logic, not waiting
    monkeypatch.setattr("time.sleep", lambda *_: None)
    media = _media()
    dest = tmp_path / "dl"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        return httpx.Response(200, content=b"video-bytes")

    client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=_CDN_TIMEOUT
    )
    out = download_media(media, dest, client=client, rate_limit=False)
    assert out.exists()
    assert out.read_bytes() == b"video-bytes"
    assert not Path(str(out) + ".part").exists()
    assert calls["n"] == 2  # first failed, second succeeded


def test_download_gives_up_after_max_attempts_on_persistent_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    media = _media()
    dest = tmp_path / "dl"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read stalled")

    client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=_CDN_TIMEOUT
    )
    with pytest.raises(httpx.ReadTimeout):
        download_media(media, dest, client=client, rate_limit=False)
    assert calls["n"] == _DOWNLOAD_ATTEMPTS
    # no truncated file at final path, no leftover .part
    assert not (dest / "videos" / "T1.mp4").exists()


def test_download_403_fails_fast_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    media = _media()
    dest = tmp_path / "dl"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403)

    client = httpx.Client(
        transport=httpx.MockTransport(handler), timeout=_CDN_TIMEOUT
    )
    with pytest.raises(httpx.HTTPStatusError):
        download_media(media, dest, client=client, rate_limit=False)
    assert calls["n"] == 1  # permanent error: single attempt


def test_backoff_schedule_is_bounded():
    # 3 attempts -> 2 sleeps (2s, 5s)
    assert len(_DOWNLOAD_BACKOFF_S) == _DOWNLOAD_ATTEMPTS - 1
