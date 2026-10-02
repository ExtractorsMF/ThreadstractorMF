"""Regression: output layout must stay predictable and documented.

The photos/videos/profile split is driven by ``dest``'s *name*, which is magic
behaviour that used to live inline in ``download_media`` with a nested branch
that could never run. These tests pin the resulting table so any future change
is deliberate.
"""

from pathlib import Path

import httpx
import pytest

import threadstractormf.downloader as dl
from threadstractormf.downloader import CONTENT_DIRS, _split_subfolder, download_media
from threadstractormf.models import Media


@pytest.fixture(autouse=True)
def _allow_local(monkeypatch):
    monkeypatch.setattr(dl, "is_valid_media_url", lambda _u: True)


def _media(media_id: str = "P1", mtype: str = "image") -> Media:
    ext = "mp4" if mtype == "video" else "jpg"
    return Media(
        id=media_id,
        post_id=media_id,
        index=1,
        type=mtype,
        url=f"https://scontent.cdninstagram.com/v/t51.1/a.{ext}?x=1",
        ext=ext,
    )


# --- the documented table ---------------------------------------------------


def test_split_by_type_for_a_plain_destination():
    assert _split_subfolder(_media(), Path("/dl")) == "photos"
    assert _split_subfolder(_media(mtype="video"), Path("/dl")) == "videos"


@pytest.mark.parametrize("name", sorted(CONTENT_DIRS))
def test_no_split_inside_a_content_directory(name):
    """A caller that already picked photos/ or posts/ gets no extra nesting."""
    dest = Path("/dl") / name
    assert _split_subfolder(_media(), dest) is None
    assert _split_subfolder(_media(mtype="video"), dest) is None


def test_avatar_goes_to_profile_unless_already_there():
    avatar = _media(media_id="user_profile")
    assert _split_subfolder(avatar, Path("/dl")) == "profile"
    assert _split_subfolder(avatar, Path("/dl/profile")) is None


def test_avatar_ignores_the_type():
    assert _split_subfolder(_media("user_profile", "video"), Path("/dl")) == "profile"


def test_video_in_a_photos_directory_is_not_moved():
    """Regression-shaped: dest='photos' used to be a way to trap videos."""
    assert _split_subfolder(_media(mtype="video"), Path("/dl/photos")) is None


def test_content_dirs_is_the_documented_set():
    assert CONTENT_DIRS == frozenset({"photos", "videos", "profile", "posts"})


# --- end to end -------------------------------------------------------------


def _download(media, dest) -> Path:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x"))
    )
    return download_media(media, dest, client=client, rate_limit=False)


def test_plain_dest_creates_photos_and_videos(tmp_path):
    image = _download(_media("P1"), tmp_path)
    video = _download(_media("P2", "video"), tmp_path)
    assert image.parent.name == "photos"
    assert video.parent.name == "videos"


def test_posts_directory_stays_flat(tmp_path):
    dest = tmp_path / "posts"
    image = _download(_media("P1"), dest)
    video = _download(_media("P2", "video"), dest)
    assert image.parent == dest
    assert video.parent == dest, "videos must not be nested inside posts/"


def test_photos_directory_keeps_the_video_flat(tmp_path):
    dest = tmp_path / "photos"
    out = _download(_media("P2", "video"), dest)
    assert out.parent == dest


def test_avatar_directory(tmp_path):
    dest = tmp_path / "dl"
    out = _download(_media("user_profile"), dest)
    assert out.parent.name == "profile"


def test_avatar_into_an_existing_profile_directory(tmp_path):
    dest = tmp_path / "profile"
    out = _download(_media("user_profile"), dest)
    assert out.parent == dest, "no profile/ inside profile/"


# --- the ledger must not fragment per subfolder -----------------------------


def test_ledger_path_is_computed_from_the_root(tmp_path):
    """One account keeps one ledger, not one per photos/ videos/ folder."""
    from threadstractormf.archive import ledger_path_for

    assert ledger_path_for(tmp_path).parent.parent == tmp_path
    assert ledger_path_for(tmp_path / "photos").parent.parent == tmp_path / "photos"


def test_ledger_records_the_final_path_with_subfolder(tmp_path):
    from threadstractormf.archive import DownloadLedger, ledger_path_for

    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    out = _download(_media("P1"), tmp_path)
    ledger.record("P1", 1, out)
    assert ledger.has("P1", 1)
    assert out.relative_to(tmp_path).parts[0] == "photos"
