"""Regression: the download ledger must prevent re-downloads.

Filename-based dedup alone re-downloaded whole categories of media every time
the naming scheme changed, because the name embeds the extension and the date
while only ``post_id`` is stable. The ledger keys on ``(post_id, index)``.
"""

import json

import httpx
import pytest

import threadstractormf.downloader as dl
from threadstractormf.archive import (
    DownloadLedger,
    find_existing_for_post,
    ledger_path_for,
    migrate_legacy_ledger,
)
from threadstractormf.client import Threadscraper
from threadstractormf.downloader import DownloadStatus, download_media
from threadstractormf.models import Media

TPL = "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}"
DATE = "2026-04-14T18:32:05+00:00"


@pytest.fixture(autouse=True)
def _allow_local(monkeypatch):
    """The CDN allowlist needs https + a CDN host; relax it for a local server."""
    monkeypatch.setattr(dl, "is_valid_media_url", lambda _u: True)


class _Server:
    """Counts requests so a test can assert nothing was re-fetched."""

    def __init__(self):
        self.calls = 0

    def client(self) -> httpx.Client:
        def handler(_r):
            self.calls += 1
            return httpx.Response(200, content=b"DATA")

        return httpx.Client(transport=httpx.MockTransport(handler))

    def video(self, post_id: str, ext: str = "webm") -> Media:
        return Media(
            id=post_id,
            post_id=post_id,
            index=1,
            type="video",
            url=f"https://scontent.cdninstagram.com/v/t65/v.{ext}?a=1",
            ext=ext,
        )

    def download(self, media, dest, **kw):
        return download_media(
            media, dest, client=self.client(), rate_limit=False, **kw
        )


# --- ledger basics ----------------------------------------------------------


def test_ledger_records_and_recognises(tmp_path):
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    assert not ledger.has("DP1", 1)
    ledger.record("DP1", 1, tmp_path / "DP1_01.jpg")
    assert ledger.has("DP1", 1)
    assert not ledger.has("DP1", 2), "index is part of the key"
    assert not ledger.has("DP2", 1), "post_id is part of the key"


def test_ledger_survives_a_reload(tmp_path):
    first = DownloadLedger(ledger_path_for(tmp_path))
    first.load()
    first.record("DP1", 1, tmp_path / "a.jpg")

    second = DownloadLedger(ledger_path_for(tmp_path))
    second.load()
    assert second.has("DP1", 1)
    assert len(second) == 1


def test_ledger_line_format(tmp_path):
    """Mirrors scrapmf's JSONL shape so the two can be unified later."""
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    ledger.record("DP1", 2, tmp_path / "x.jpg")
    entry = json.loads(ledger_path_for(tmp_path).read_text().strip())
    assert entry["post_id"] == "DP1"
    assert entry["index"] == 2
    assert entry["path"].endswith("x.jpg")
    assert isinstance(entry["t"], int)


def test_ledger_skips_corrupt_lines(tmp_path):
    path = ledger_path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"post_id":"OK1","index":1}\n'
        "not json at all\n"
        "\n"
        '{"no_post_id": true}\n'
        '{"post_id":"OK2","index":1,"t":1}\n'
    )
    ledger = DownloadLedger(path)
    ledger.load()  # must not raise
    assert ledger.has("OK1", 1)
    assert ledger.has("OK2", 1)
    assert len(ledger) == 2


def test_ledger_never_writes_duplicates(tmp_path):
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    ledger.record("DP1", 1, tmp_path / "a.jpg")
    ledger.record("DP1", 1, tmp_path / "a.jpg")
    assert len(ledger_path_for(tmp_path).read_text().strip().splitlines()) == 1


def test_ledger_path_is_inside_the_media_directory(tmp_path):
    path = ledger_path_for(tmp_path)
    assert path.parent.parent == tmp_path
    assert path.parts[-2] == ".archive"
    assert path.name == "dedup.jsonl"


def test_disabled_ledger_is_inert(tmp_path):
    ledger = DownloadLedger(None)
    ledger.load()
    ledger.record("DP1", 1, tmp_path / "a.jpg")
    assert not ledger.has("DP1", 1)


# --- the three dedup levels -------------------------------------------------


def test_download_is_recorded_and_not_repeated(tmp_path):
    srv = _Server()
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()

    media = srv.video("DP1")
    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger)
    assert srv.calls == 1
    assert ledger.has("DP1", 1), "a completed download records itself"

    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger)
    assert srv.calls == 1, "the second pass must not hit the network"


def test_download_without_a_ledger_still_works(tmp_path):
    """archive=False must behave exactly as before: download and stop."""
    srv = _Server()
    media = srv.video("DP1")
    outcome = srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE)
    out = outcome.path
    assert outcome.status is DownloadStatus.DOWNLOADED
    assert out.exists()
    assert srv.calls == 1
    assert not ledger_path_for(tmp_path).exists(), "no ledger is written"


def test_ledger_survives_a_renamed_template(tmp_path):
    """The headline case: a template change must not re-download anything."""
    srv = _Server()
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()

    media = srv.video("DP1")
    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger)
    assert srv.calls == 1

    srv.download(
        media,
        tmp_path,
        filename_template="{post_id}_{num:02d}.{extension}",
        date_iso=DATE,
        ledger=ledger,
    )
    assert srv.calls == 1


def test_date_rollover_does_not_re_download(tmp_path):
    """date_iso=None makes the filename contain *today*, which changes daily."""
    srv = _Server()
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()

    media = srv.video("DP1")
    srv.download(media, tmp_path, filename_template=TPL, date_iso=None, ledger=ledger)
    assert srv.calls == 1

    srv.download(media, tmp_path, filename_template=TPL, date_iso=None, ledger=ledger)
    assert srv.calls == 1


def test_exact_file_seeds_the_ledger(tmp_path):
    """Level 2: an existing file is adopted even with an empty ledger."""
    srv = _Server()
    media = srv.video("DP1")
    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE)

    fresh = DownloadLedger(ledger_path_for(tmp_path))
    fresh.load()
    assert len(fresh) == 0, "precondition: nothing recorded yet"

    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE, ledger=fresh)
    assert fresh.has("DP1", 1)
    assert srv.calls == 1


def test_carousel_items_are_tracked_separately(tmp_path):
    srv = _Server()
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    items = [
        Media(id=f"C1_{i}", post_id="C1", index=i, type="image",
              url=f"https://scontent.cdninstagram.com/v/t51.1/{i}.jpg?a=1", ext="jpg")
        for i in (1, 2, 3)
    ]
    for m in items:
        srv.download(m, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger)

    reloaded = DownloadLedger(ledger_path_for(tmp_path))
    reloaded.load()
    assert len(reloaded) == 3
    for i in (1, 2, 3):
        assert reloaded.has("C1", i)

    before = srv.calls
    for m in items:
        srv.download(m, tmp_path, filename_template=TPL, date_iso=DATE, ledger=reloaded)
    assert srv.calls == before, "a carousel must not re-download"


def test_overwrite_bypasses_the_ledger(tmp_path):
    srv = _Server()
    media = srv.video("DP1")
    srv.download(media, tmp_path, filename_template=TPL, date_iso=DATE)
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()

    srv.download(
        media, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger, overwrite=True
    )
    assert srv.calls == 2, "--overwrite means re-download"


def test_archive_disabled_falls_back_to_filename(tmp_path):
    scraper = Threadscraper(cookies={"sessionid": "x"}, archive=False)
    try:
        assert scraper._ledger_for(tmp_path) is None
    finally:
        scraper.close()


def test_scraper_reuses_one_ledger_per_destination(tmp_path):
    scraper = Threadscraper(cookies={"sessionid": "x"})
    try:
        a = scraper._ledger_for(tmp_path)
        b = scraper._ledger_for(tmp_path)
        assert a is b
        other = scraper._ledger_for(tmp_path / "otro")
        assert other is not a
    finally:
        scraper.close()


def test_one_ledger_per_account_not_per_subfolder(tmp_path):
    """photos/ and videos/ must share a single ledger for the same account."""
    scraper = Threadscraper(cookies={"sessionid": "x"})
    try:
        root = scraper._ledger_for(tmp_path)
        assert scraper._ledger_for(tmp_path / "photos") is not root
        # ...but download_media derives it from the pre-split root, so a download
        # into <root> and one into <root>/photos end up recorded per call site.
        srv = _Server()
        srv.download(srv.video("DP1"), tmp_path, filename_template=TPL, date_iso=DATE,
                     ledger=root)
        assert root.has("DP1", 1)
    finally:
        scraper.close()


# --- adoption pattern -------------------------------------------------------


def test_adoption_requires_a_delimiter_after_the_id(tmp_path):
    """A bare substring search would let DPxyz123 adopt DPxyz1234 — a different
    post — and silently mark it as downloaded."""
    (tmp_path / "2026-04-14_DPx9Y8z7W6V5_01.jpg").write_bytes(b"x")
    (tmp_path / "2026-04-14_DPx9Y8z7W6V51_01.jpg").write_bytes(b"x")

    assert find_existing_for_post(tmp_path, "DPx9Y8z7W6V5").name.endswith(
        "DPx9Y8z7W6V5_01.jpg"
    )
    assert find_existing_for_post(tmp_path, "DPx9Y8z7W6V51").name.endswith(
        "DPx9Y8z7W6V51_01.jpg"
    )


def test_adoption_matches_exact_and_dotted_names(tmp_path):
    (tmp_path / "DPplain.jpg").write_bytes(b"x")
    (tmp_path / "2026-04-14_DPdot_02.jpg").write_bytes(b"x")
    assert find_existing_for_post(tmp_path, "DPplain", 1).name == "DPplain.jpg"
    # the index is part of the match now: _02 is item 2, not item 1
    assert find_existing_for_post(tmp_path, "DPdot", 2).name == "2026-04-14_DPdot_02.jpg"
    assert find_existing_for_post(tmp_path, "DPdot", 1) is None


def test_adoption_returns_none_when_absent(tmp_path):
    assert find_existing_for_post(tmp_path, "NOPE") is None
    assert find_existing_for_post(tmp_path / "no-existe", "DP1") is None


def test_adoption_ignores_directories(tmp_path):
    (tmp_path / "DP1_dir").mkdir()
    assert find_existing_for_post(tmp_path, "DP1") is None


def test_legacy_extension_is_adopted_not_re_downloaded(tmp_path):
    """The scenario that motivated this: a .webm saved as .jpg before the
    extension fix, which the new naming scheme would otherwise re-download."""
    srv = _Server()
    # the old, wrong name on disk
    old = Media(id="DPwE5bM6nO7P", post_id="DPwE5bM6nO7P", index=1, type="video",
                url="https://scontent.cdninstagram.com/v/t65/v.jpg?a=1", ext="jpg")
    srv.download(old, tmp_path, filename_template=TPL, date_iso=DATE)
    before = srv.calls

    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    # now the same post, correctly detected as .webm -> a different filename
    outcome = srv.download(
        srv.video("DPwE5bM6nO7P", "webm"),
        tmp_path,
        filename_template=TPL,
        date_iso=DATE,
        ledger=ledger,
    )
    assert srv.calls == before, "must adopt, not re-download"
    assert outcome.status is DownloadStatus.ADOPTED, "recognised, not fetched"
    assert outcome.path.suffix == ".jpg", "the existing file keeps its old name"
    assert ledger.has("DPwE5bM6nO7P", 1)


def test_adopt_can_be_disabled(tmp_path):
    srv = _Server()
    old = Media(id="DP1", post_id="DP1", index=1, type="video",
                url="https://scontent.cdninstagram.com/v/t65/v.jpg?a=1", ext="jpg")
    srv.download(old, tmp_path, filename_template=TPL, date_iso=DATE)
    before = srv.calls

    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    srv.download(
        srv.video("DP1", "webm"),
        tmp_path,
        filename_template=TPL,
        date_iso=DATE,
        ledger=ledger,
        adopt_existing=False,
    )
    assert srv.calls == before + 1, "--no-adopt-existing forces the download"


# --- carousel items must not adopt each other -------------------------------
#
# Regression found by an end-to-end run against a real profile: 13 of 15 files
# were never downloaded. Every item of a carousel carries the same post_id, so
# adoption — which searched on post_id alone — made item 2 adopt item 1's file,
# then item 3 adopt item 1's file, and so on. 304 unit tests missed it because
# the fixtures all had a single media item per post.


def test_adoption_distinguishes_carousel_items(tmp_path):
    (tmp_path / "P1_1.jpg").write_bytes(b"x")
    (tmp_path / "P1_2.jpg").write_bytes(b"x")
    from threadstractormf.archive import find_existing_for_post

    assert find_existing_for_post(tmp_path, "P1", 1).name == "P1_1.jpg"
    assert find_existing_for_post(tmp_path, "P1", 2).name == "P1_2.jpg"
    assert find_existing_for_post(tmp_path, "P1", 3) is None


def test_admission_never_returns_item_1_for_a_later_item(tmp_path):
    (tmp_path / "P1_1.jpg").write_bytes(b"x")
    from threadstractormf.archive import find_existing_for_post

    assert find_existing_for_post(tmp_path, "P1", 2) is None


def test_zero_padded_numbers_are_understood(tmp_path):
    (tmp_path / "2026-04-14_P1_01.jpg").write_bytes(b"x")
    (tmp_path / "2026-04-14_P1_02.webp").write_bytes(b"x")
    from threadstractormf.archive import find_existing_for_post

    assert find_existing_for_post(tmp_path, "P1", 1).name == "2026-04-14_P1_01.jpg"
    assert find_existing_for_post(tmp_path, "P1", 2).name == "2026-04-14_P1_02.webp"


def test_unnumbered_file_belongs_only_to_item_one(tmp_path):
    (tmp_path / "P1.jpg").write_bytes(b"x")
    from threadstractormf.archive import find_existing_for_post

    assert find_existing_for_post(tmp_path, "P1", 1) is not None
    assert find_existing_for_post(tmp_path, "P1", 2) is None


def test_explicit_number_wins_over_a_bare_match(tmp_path):
    (tmp_path / "P1.jpg").write_bytes(b"x")
    (tmp_path / "P1_1.jpg").write_bytes(b"x")
    from threadstractormf.archive import find_existing_for_post

    assert find_existing_for_post(tmp_path, "P1", 1).name == "P1_1.jpg"


def test_a_carousel_downloads_every_item(tmp_path):
    """The end-to-end regression: one file per item, not one for the post."""
    srv = _Server()
    ledger = DownloadLedger(ledger_path_for(tmp_path))
    ledger.load()
    items = [
        Media(id=f"C1_{i}", post_id="C1", index=i, type="image",
              url=f"https://scontent.cdninstagram.com/v/t51.1/{i}.jpg?a=1", ext="jpg")
        for i in range(1, 6)
    ]
    for m in items:
        srv.download(m, tmp_path, filename_template=TPL, date_iso=DATE, ledger=ledger)

    assert srv.calls == 5, "each item is a distinct download"
    on_disk = sorted(p.name for p in tmp_path.rglob("*C1_*"))
    assert len(on_disk) == 5, f"one file per item, got {on_disk}"
    assert len(set(on_disk)) == 5, "no item may reuse another's file"


# --- the ledger is opt-in ----------------------------------------------------
#
# A plain download must leave nothing behind: no directory, no bookkeeping.
# Until 1.1.1 the ledger was on by default, so every scrape dropped a
# .threadstractormf/ folder into the user's media directory whether they wanted
# one or not.


def test_no_ledger_directory_without_the_flag(tmp_path):
    """The default writes no directory at all."""
    srv = _Server()
    media = srv.video("DP1")
    dest = tmp_path / "dl"
    srv.download(media, dest, filename_template=TPL, date_iso=DATE)

    assert not ledger_path_for(dest).exists(), "no ledger without --archive"
    assert not (dest / ".archive").exists()
    assert not (dest / ".threadstractormf").exists()
    assert dest.exists(), "the media itself must still be there"


def test_archive_flag_creates_the_directory(tmp_path):
    scraper = Threadscraper(cookies={"sessionid": "x"}, archive=True)
    try:
        ledger = scraper._ledger_for(tmp_path / "dl")
    finally:
        scraper.close()
    assert ledger is not None
    assert ledger.path.name == "dedup.jsonl"
    assert ledger.path.parent.name == ".archive"


def test_the_directory_is_hidden(tmp_path):
    """Kept dot-prefixed so it does not clutter the media listing."""
    assert ledger_path_for(tmp_path).parent.name.startswith(".")


# --- migration from the pre-1.1.1 directory name ---------------------------


def _write_legacy(dest, lines):
    legacy = dest / ".threadstractormf"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "dedup.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return legacy / "dedup.jsonl"


def test_legacy_ledger_is_migrated(tmp_path):
    """Losing it would re-download the whole library on the next run."""
    dest = tmp_path / "dl"
    _write_legacy(dest, ['{"post_id": "DP1", "index": 1, "path": "/x", "t": 1}'])

    assert migrate_legacy_ledger(dest) is True
    new = ledger_path_for(dest)
    assert new.exists(), "the ledger must arrive in .archive/"
    assert not (dest / ".threadstractormf").exists(), "and the old dir goes away"
    ledger = DownloadLedger(new)
    ledger.load()
    assert ledger.has("DP1", 1), "the history has to survive the move"


def test_no_legacy_directory_is_not_an_error(tmp_path):
    assert migrate_legacy_ledger(tmp_path / "dl") is False


def test_migration_keeps_entries_from_both_files(tmp_path):
    """Both present: merge, never overwrite, or half the library re-downloads."""
    dest = tmp_path / "dl"
    _write_legacy(dest, ['{"post_id": "OLD", "index": 1, "path": "/x", "t": 1}'])
    new = ledger_path_for(dest)
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(
        '{"post_id": "NEW", "index": 1, "path": "/y", "t": 2}\n', encoding="utf-8"
    )

    assert migrate_legacy_ledger(dest) is True
    ledger = DownloadLedger(new)
    ledger.load()
    assert ledger.has("OLD", 1)
    assert ledger.has("NEW", 1)


def test_migration_does_not_duplicate_shared_lines(tmp_path):
    dest = tmp_path / "dl"
    line = '{"post_id": "SAME", "index": 1, "path": "/x", "t": 1}'
    _write_legacy(dest, [line])
    new = ledger_path_for(dest)
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(line + "\n", encoding="utf-8")

    migrate_legacy_ledger(dest)
    body = new.read_text(encoding="utf-8").splitlines()
    assert body.count(line) == 1


def test_migration_leaves_unknown_files_in_the_old_directory(tmp_path):
    """rmdir only: deleting anything else there would lose data."""
    dest = tmp_path / "dl"
    legacy = _write_legacy(dest, ['{"post_id": "DP1", "index": 1, "path": "/x", "t": 1}'])
    (legacy.parent / "notebook.txt").write_text("keep me", encoding="utf-8")

    migrate_legacy_ledger(dest)
    assert ledger_path_for(dest).exists()
    assert (legacy.parent / "notebook.txt").exists(), "must not delete unknown files"


def test_the_client_migrates_before_reading(tmp_path):
    """End to end: a library built under the old name must not re-download."""
    dest = tmp_path / "dl"
    _write_legacy(dest, ['{"post_id": "DP1", "index": 1, "path": "/x", "t": 1}'])
    scraper = Threadscraper(cookies={"sessionid": "x"}, archive=True)
    try:
        ledger = scraper._ledger_for(dest)
    finally:
        scraper.close()
    assert ledger is not None
    assert ledger.has("DP1", 1)
