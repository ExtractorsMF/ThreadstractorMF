"""Regression: video_versions extraction from live DOM HTML.

Meta now emits the first object with other keys before "url"
(e.g. {"type":101,"url":"..."}) and escapes slashes (https:\\/\\/...).
The old regex /"video_versions":\\s*\\[\\s*\\{"url":"([^"]+)"/ matched nothing.
"""

import re

# real sample shape from threads.com DOM (URL truncated/sanitized)
RAW_SAMPLE = (
    '"video_versions":[{"type":101,"url":"https:\\/\\/instagram.fpei1-1.fna.fbcdn.net'
    '\\/o1\\/v\\/t16\\/f2\\/m84\\/VIDEO_HASH.mp4?_nc_cat=101\\u0026_nc_sid=5e9851'
    '\\u0026efg=eyJ2ZW5jb2RlX3RhZyI6InhwdiJ9"}]'
)

NULL_SAMPLE = '"video_versions":null,"usertags":null'


def _extract_urls(html: str) -> list[str]:
    """Same logic as the fixed JS embedded in api.py."""
    urls: list[str] = []
    for m in re.finditer(r'"video_versions":\[([^\]]*)', html):
        um = re.search(r'"url":"([^"]+)"', m.group(1))
        if not um:
            continue
        u = um.group(1)
        u = u.replace("\\u0026", "&").replace("\\u0025", "%").replace("\\/", "/")
        urls.append(u)
    return urls


def test_old_regex_matches_nothing_on_new_format():
    assert not re.search(r'"video_versions":\s*\[\s*\{"url":"([^"]+)"', RAW_SAMPLE)


def test_new_format_extracts_url_with_leading_keys():
    urls = _extract_urls(RAW_SAMPLE)
    assert len(urls) == 1
    assert urls[0].startswith("https://instagram.fpei1-1.fna.fbcdn.net/o1/v/t16/")
    assert urls[0].endswith(".mp4?_nc_cat=101&_nc_sid=5e9851&efg=eyJ2ZW5jb2RlX3RhZyI6InhwdiJ9")
    # no escaped separators left
    assert "\\u0026" not in urls[0]
    assert "\\/" not in urls[0]


def test_null_video_versions_skipped():
    assert _extract_urls(NULL_SAMPLE) == []


def test_extracted_url_passes_media_allowlist():
    from threadstractormf.downloader import is_valid_media_url

    url = _extract_urls(RAW_SAMPLE)[0]
    assert is_valid_media_url(url)
