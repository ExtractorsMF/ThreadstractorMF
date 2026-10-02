"""Regression: the Brave variants each load their own profile.

``browser_cookie3.brave()`` only resolves the default ``Brave-Browser`` profile.
A user whose live session lives in ``Brave-Origin`` — a separate install with
its own config root — got the stale jar instead, with nothing to indicate it:
same five cookie names, no ``rur`` or ``cb``, and a session that simply fails.

This is the failure mode that would make a working extractor look broken.
"""

import http.cookiejar
import sys
from unittest import mock

import pytest

from threadstractormf.auth import load_from_browser


class _FakeBrave:
    """Stands in for browser_cookie3, recording which file each variant reads."""

    def __init__(self):
        self.read_files: list[str] = []

    @staticmethod
    def _make_jar(domain_name=""):
        jar = http.cookiejar.CookieJar()
        jar.set_cookie(
            http.cookiejar.Cookie(
                0, "sessionid", f"v-{domain_name}", None, False,
                ".threads.net", True, True, "/", True, True, None, False,
                None, None, None, {},
            )
        )
        return jar

    def brave(self, domain_name=""):
        self.read_files.append("SHORTCUT")
        return self._make_jar(domain_name)

    def Brave(self, cookie_file=None, domain_name="", key_file=None):  # noqa: N802
        def _load():
            self.read_files.append(cookie_file or "")
            return self._make_jar(domain_name)

        return mock.Mock(load=_load)


@pytest.fixture
def fake_bc(tmp_path, monkeypatch):
    """Pretend every Brave profile is installed, so each variant resolves."""
    for sub in ("Brave-Browser", "Brave-Origin", "Brave-Beta", "Brave-Browser-Nightly"):
        default = tmp_path / ".config" / "BraveSoftware" / sub / "Default"
        default.mkdir(parents=True)
        (default / "Cookies").write_bytes(b"")
        (tmp_path / ".config" / "BraveSoftware" / sub / "Local State").write_text("{}")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    fake = _FakeBrave()
    with mock.patch.dict(sys.modules, {"browser_cookie3": fake}):
        yield fake


def test_brave_shortcut_reads_the_default_profile(fake_bc):
    # Two calls: threads.com, then threads.net (the session spans both).
    load_from_browser("brave")
    assert fake_bc.read_files == ["SHORTCUT", "SHORTCUT"]


@pytest.mark.parametrize(
    ("variant", "profile_dir"),
    [
        ("brave-origin", "Brave-Origin"),
        ("brave-beta", "Brave-Beta"),
        ("brave-nightly", "Brave-Browser-Nightly"),
    ],
)
def test_variants_use_explicit_paths(fake_bc, variant, profile_dir):
    """Each variant must point at its own config subdirectory."""
    load_from_browser(variant)
    assert fake_bc.read_files, "the loader was never called"
    for path in fake_bc.read_files:
        assert "SHORTCUT" not in path, "must not fall back to the default-profile shortcut"
        assert path.endswith(f"BraveSoftware/{profile_dir}/Default/Cookies")


def test_origin_and_default_are_different_sources(fake_bc):
    load_from_browser("brave")
    load_from_browser("brave-origin")
    assert set(fake_bc.read_files[:2]) == {"SHORTCUT"}
    assert all("Brave-Origin" in p for p in fake_bc.read_files[2:])


def test_brave_browser_alias_matches_brave(fake_bc):
    load_from_browser("brave-browser")
    assert set(fake_bc.read_files) == {"SHORTCUT"}


def test_missing_profile_raises_a_clear_error(tmp_path, monkeypatch):
    """A missing profile must say which path was looked for."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        load_from_browser("brave-origin")
    message = str(excinfo.value)
    assert "Brave-Origin" in message
    assert "Brave-Origin" in str(excinfo.value)


def test_variants_are_accepted_in_the_error_message(fake_bc):
    """The error lists what *is* supported, so the variants must be in it."""
    with pytest.raises(ValueError) as excinfo:
        load_from_browser("netscape")
    message = str(excinfo.value)
    for expected in ("brave", "brave-origin", "brave-beta", "brave-nightly"):
        assert expected in message


def test_case_insensitive(fake_bc):
    load_from_browser("BRAVE-ORIGIN")
    assert all("Brave-Origin" in p for p in fake_bc.read_files)


def test_other_browsers_are_unaffected(fake_bc):
    """The non-Brave loaders still use browser_cookie3's own shortcuts."""
    fake_bc.chrome = lambda domain_name="": fake_bc._make_jar(domain_name)
    jar = load_from_browser("chrome")
    assert {c.name for c in jar} == {"sessionid"}
