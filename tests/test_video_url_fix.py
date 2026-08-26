"""Regression: decoding of \\u0026 in video URLs extracted from HTML (Playwright fallback).

Bug: JS String.replace(string) only replaces the FIRST occurrence. video_versions
URLs have many query params separated by \\u0026 -> corrupted URL -> CDN 403.
Fix: replaceAll with a global regex.
"""

import re

# Simulates what the embedded JS in api.py does on a JSON match from the HTML
_RAW_URL = (
    "https://scontent.cdninstagram.com/o1/v/t16/f2/m86/video.mp4"
    "?efg=eyJ\\u0026_nc_ht=scontent.cdninstagram.com\\u0026_nc_cat=111\\u0026edm=ABCDEF\\u0026oh=00_xyz\\u0026oe=64FA00"
)

# same logic as the fixed JS in api.py (replaceAll(/\\u0026/g,"&"))
_JS_FIXED = re.compile(r"\\u0026").sub("&", _RAW_URL)


def test_video_url_all_ampersands_decoded():
    assert "\\u0026" not in _JS_FIXED
    assert _JS_FIXED.count("&") == 5


def test_video_url_is_valid_media_url_after_decode():
    from threadstractormf.downloader import is_valid_media_url

    # the decoded URL must pass the allowlist and be downloadable in theory
    assert is_valid_media_url(_JS_FIXED)
    # with leftover escapes it ALSO passes validation (why the bug was silent:
    # it passed is_valid_media_url but failed when downloading from the CDN)
    assert is_valid_media_url(_RAW_URL)
