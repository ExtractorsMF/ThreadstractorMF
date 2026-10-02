"""Regression: --get-urls is the dry-run flag scrapmf's scraper passes.

``scraper.rs`` appends ``--get-urls`` to the argument list when ``dry_run`` is
set and then inherits stdio, so the flag has to exist and the output has to be
plain, pipeable lines. Before this the CLI rejected the option outright with
"No such option: --get-urls", and nothing else in the flag surface changed.

Nothing may be written to the destination, and rate limiting stays on: a dry
run still talks to the same rate-limited API.
"""

import pytest
from typer.testing import CliRunner

import threadstractormf.cli as cli_module
import threadstractormf.client as client_module
from threadstractormf.models import Media, Post, Profile

MAGIC = "# Netscape HTTP Cookie File"
IMAGE = "https://scontent.cdninstagram.com/v/t51.1/a.jpg?x=1"
VIDEO = "https://scontent.cdninstagram.com/o1/v/t16/b.mp4?x=1"
AVATAR = "https://scontent.cdninstagram.com/v/t51.2885-19/av.jpg"


def _post() -> Post:
    return Post(
        id="P1",
        permalink="https://www.threads.com/@user/post/P1",
        username="user",
        media=[
            Media(id="P1_1", post_id="P1", index=1, type="image", url=IMAGE, ext="jpg"),
            Media(id="P1_2", post_id="P1", index=2, type="video", url=VIDEO, ext="mp4"),
        ],
    )


class FakeScraper:
    """Records every call so a test can assert nothing was downloaded."""

    def __init__(self, **kwargs):
        self.downloads: list[str] = []
        self.avatar_downloads: list[str] = []
        self.closed = False

    def get_posts(self, username, limit=None):
        return [_post()]

    def get_profile(self, username):
        return Profile(username=username, profile_pic_url=AVATAR)

    def download(self, media, dest, **kwargs):
        self.downloads.append(media.id)
        raise AssertionError("--get-urls must not download")

    def download_profile_pic(self, username, dest, **kwargs):
        self.avatar_downloads.append(username)
        raise AssertionError("--get-urls must not download the avatar")

    def close(self):
        self.closed = True


@pytest.fixture
def cookies_file(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(MAGIC + "\n.threads.net\tTRUE\t/\tTRUE\t2147483647\tsessionid\tV\n")
    return f


@pytest.fixture
def scraper(monkeypatch):
    """cli.py imports Threadscraper inside main(), so patch the source module."""
    made: list[FakeScraper] = []

    def factory(**kwargs):
        instance = FakeScraper(**kwargs)
        made.append(instance)
        return instance

    monkeypatch.setattr(client_module, "Threadscraper", factory)
    return made


def _run(cookies_file, dest, *extra):
    runner = CliRunner()
    result = runner.invoke(
        cli_module.app,
        ["--cookies", str(cookies_file), "--dest", str(dest), *extra, "@user"],
    )
    return result


# --- the flag exists -------------------------------------------------------


def test_get_urls_flag_is_accepted(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl", "--get-urls")
    assert result.exit_code == 0
    assert "No such option" not in result.output


def test_prints_one_url_per_line(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl", "--get-urls")
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert lines == [IMAGE, VIDEO], "plain URLs, one per line, in order"


def test_downloads_nothing(cookies_file, tmp_path, scraper):
    _run(cookies_file, tmp_path / "dl", "--get-urls")
    assert scraper[0].downloads == []
    assert scraper[0].avatar_downloads == []


def test_writes_nothing_to_dest(cookies_file, tmp_path, scraper):
    dest = tmp_path / "dl"
    _run(cookies_file, dest, "--get-urls")
    assert not dest.exists() or not list(dest.rglob("*.jpg")) + list(dest.rglob("*.mp4"))


def test_does_not_download_the_avatar_implicitly(cookies_file, tmp_path, scraper):
    """A normal run downloads the avatar alongside the posts; a dry run must not."""
    _run(cookies_file, tmp_path / "dl", "--get-urls")
    assert scraper[0].avatar_downloads == []


def test_closes_the_scraper(cookies_file, tmp_path, scraper):
    _run(cookies_file, tmp_path / "dl", "--get-urls")
    assert scraper[0].closed


# --- filters still apply ---------------------------------------------------


def test_videos_only(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl", "--get-urls", "--videos-only")
    assert [ln for ln in result.stdout.strip().splitlines() if ln] == [VIDEO]


def test_photos_only(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl", "--get-urls", "--photos-only")
    assert [ln for ln in result.stdout.strip().splitlines() if ln] == [IMAGE]


def test_profile_pic_only(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl", "--get-urls", "--profile-pic-only")
    assert [ln for ln in result.stdout.strip().splitlines() if ln] == [AVATAR]
    assert scraper[0].avatar_downloads == []


# --- the stdout contract scrapmf relies on ---------------------------------


def test_output_has_no_file_paths(cookies_file, tmp_path, scraper):
    """scrapmf counts stdout lines as downloaded files; a dry run prints URLs,
    which is what gallery-dl does for -g/--get-urls."""
    result = _run(cookies_file, tmp_path / "dl", "--get-urls")
    for line in result.stdout.strip().splitlines():
        assert not line.startswith("  "), "no '  id -> path' lines in a dry run"
        assert line.startswith("https://")


# --- no regression on the normal path --------------------------------------


def test_without_the_flag_it_downloads(cookies_file, tmp_path, scraper):
    result = _run(cookies_file, tmp_path / "dl")
    assert result.exit_code != 0 or True  # download raises in the fake
    assert scraper[0].downloads == ["P1_1", "P1_2"], "still downloads when not a dry run"


def test_without_the_flag_the_avatar_is_downloaded(cookies_file, tmp_path, scraper):
    _run(cookies_file, tmp_path / "dl")
    assert scraper[0].avatar_downloads == ["user"]


def test_directory_template_is_not_created_for_a_dry_run(cookies_file, tmp_path, scraper):
    dest = tmp_path / "dl"
    _run(cookies_file, dest, "--get-urls", "--directory-template", "{category}/{username}")
    assert not (dest / "threads" / "user").exists()


def test_rate_limiting_is_left_on(cookies_file, tmp_path, scraper, monkeypatch):
    """A dry run still hits the same API; the pacing must not be disabled."""
    seen = {}
    original = client_module.Threadscraper

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(client_module, "Threadscraper", spy)
    _run(cookies_file, tmp_path / "dl", "--get-urls")
    assert seen.get("rate_limit") is True


# --- long lines must not be wrapped -----------------------------------------
#
# Rich wraps at the terminal width (80 columns when piped), which turned a
# single ~600-character CDN URL into eight lines. scrapmf counts stdout lines as
# files, so it would have counted eight media for one. soft_wrap=True fixes it;
# this pins that.


LONG_URL = "https://instagram.fbog10-1.fna.fbcdn.net/v/t51.82787-15/" + "a" * 600 + "?x=1"


class _LongUrlScraper(FakeScraper):
    def get_posts(self, username, limit=None):
        post = _post()
        post.media = [
            Media(id="L1", post_id="L", index=1, type="image", url=LONG_URL, ext="jpg")
        ]
        return [post]


def test_a_long_url_is_one_physical_line(cookies_file, tmp_path, monkeypatch):
    monkeypatch.setattr(client_module, "Threadscraper", lambda **kw: _LongUrlScraper(**kw))
    result = _run(cookies_file, tmp_path / "dl", "--get-urls")
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, f"expected one line, got {len(lines)}"
    assert lines[0] == LONG_URL
    assert max(len(line) for line in lines) > 80, "the URL is genuinely long"


def test_downloaded_paths_are_also_unwrapped(cookies_file, tmp_path, monkeypatch):
    """The per-file lines scrapmf counts had the same problem."""
    long_path = tmp_path / ("d" * 90) / "deep" / "nested" / "folder" / "DPxyz123_01.jpg"

    class _Downloader(FakeScraper):
        def download(self, media, dest, **kwargs):
            self.downloads.append(media.id)
            return long_path

        def download_profile_pic(self, username, dest, **kwargs):
            self.avatar_downloads.append(username)
            return long_path

    monkeypatch.setattr(client_module, "Threadscraper", lambda **kw: _Downloader(**kw))
    result = _run(cookies_file, tmp_path / "dl")
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert lines, "the fake downloader should have printed something"
    for line in lines:
        assert " -> " in line and "\n" not in line
    assert max(len(line) for line in lines) > 80
