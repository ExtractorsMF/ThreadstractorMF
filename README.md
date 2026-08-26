# threadstractormf

Threads media downloader — `import threadstractormf` + CLI like `gallery-dl` / `yt-dlp`.

- **Auth** via Netscape `cookies.txt` (`MozillaCookieJar`) — same file as `gallery-dl --cookies` / `yt-dlp --cookies`
- **Media IDs** `post_id` (`/post/<code>`) + `_1/_2/_3` for carousels, no reposts
- **Media** photos, videos, profile pic (`t51.2885-19`)
- **Stack** `httpx[http2]` + `pydantic` + optional `curl_cffi` (TLS/JA4 impersonation) + `browser-cookie3` + `playwright` fallback, `uv`/`hatchling`, `requires-python >=3.10`

## Installation

```bash
# from source
uv pip install -e ".[dev]"          # dev: pytest, respx, ruff, mypy
uv pip install -e ".[antibot]"      # TLS bypass (curl_cffi)
uv pip install -e ".[browser]"      # Playwright + browser-cookie3

# pip
pip install threadstractormf
which threadstractormf  # ~/.local/bin/threadstractormf
```

## Library

```python
import browser_cookie3
from threadstractormf import Threadscraper

# Brave (gallery-dl compatible)
scraper = Threadscraper(cookies=browser_cookie3.brave(domain_name="threads.com"))
# or Netscape file
scraper = Threadscraper(cookies="cookies.txt")
# or dict
scraper = Threadscraper(cookies={"sessionid": "...", "csrftoken": "..."})

posts = scraper.get_posts("user", limit=20)  # exclude_reposts=True by default
for post in posts:
    for media in post.media:  # media.id = "DK123" or "DK123_1"
        scraper.download(media, dest="./dl",
            filename_template="{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}",
            username=post.username, date_iso=post.datetime_iso)

scraper.download_profile_pic("user", dest="./dl")

# rate-limit configurable (port of threads-downloader background.js)
scraper = Threadscraper(cookies="cookies.txt", rate_limit=True,
                        cooldown_ms=4000, cooldown_after_100_ms=120000, rps=0.5)
```

**Models:** `Post(id=post_id, permalink, username, caption, datetime_iso, media: list[Media])`, `Media(id, post_id, index, type, url, ext)`, `Profile(username, user_id, profile_pic_url)`.

## CLI

```bash
# gallery-dl compatible cookies
threadstractormf --cookies cookies.txt --dest ./dl @user
threadstractormf --cookies-from-browser brave --dest ./dl @user
threadstractormf --cookies cookies.txt --cookies-from-browser brave --dest ./dl https://www.threads.com/@user/media

threadstractormf --cookies cookies.txt --limit 50 --dest ./dl @user
threadstractormf --cookies-from-browser brave --profile-pic-only --dest ./dl @user
threadstractormf --cookies cookies.txt --impersonate chrome --dest ./dl @user  # curl_cffi

# filename / directory templates (like gallery-dl, chronological by default)
threadstractormf --cookies cookies.txt \
  --filename-template "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}" \
  --directory-template "{scrapmf_root}/{category}/{username}/{subcategory}" \
  --dest ./dl @user

# no date, post_id only
threadstractormf --cookies cookies.txt --filename-template "{post_id}_{num:02d}.{extension}" --dest ./dl @user
```

### Options

```
target                          @username or https://www.threads.com/@user/media
-c, --cookies <path>            Netscape cookies.txt (gallery-dl compatible)
    --cookies-from-browser <str> brave|chrome|firefox|edge (reads local DB)
-d, --dest <path>               output dir [default: ./dl]
-l, --limit <int>               posts limit
    --profile-pic-only          only avatar
    --impersonate <str>         chrome (requires curl_cffi)
    --overwrite
    --no-rate-limit             disable anti rate-limit (not recommended)
    --cooldown <int>            ms between downloads [default: 2000]
    --batch-cooldown <int>      ms each 100 downloads [default: 120000]
    --rps <float>               req/s for GraphQL API [default: 0.5]
    --filename-template <str>   e.g. "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}"
    --directory-template <str>  e.g. "{scrapmf_root}/{category}/{username}/{subcategory}"
```

**Template variables:** `{post_id}`, `{media_id}` (=`post_id_1`), `{date:%Y-%m-%d}`, `{date:%Y}`, `{num}`, `{num:02d}`, `{username}`, `{category}=threads`, `{subcategory}=posts|profile`, `{extension}`.

## Cookies (gallery-dl compatible)

Export in Brave/Chrome with **Get cookies.txt LOCALLY** while logged in at `threads.com`. File must start with `# Netscape HTTP Cookie File`, fields TAB-separated: `domain TRUE/FALSE path TRUE/FALSE expires name value`. `.threads.com TRUE` sends to subdomains/CDN.

One file works for `gallery-dl`, `yt-dlp` and `threadstractormf`:
```bash
gallery-dl --cookies cookies.txt URL
yt-dlp --cookies cookies.txt URL
threadstractormf --cookies cookies.txt @user
# or from browser (desktop only, copy DB to /tmp if locked)
threadstractormf --cookies-from-browser brave @user
```

## Anti rate-limit / Anti-bot

Port of `threads-downloader/background.js` + `gallery-dl sleep`:
- **Downloads CDNs** `scontent.cdninstagram.com`: `cooldown 2s` + `120s each 100` + `jitter 20%` ( → `3-5s` with `4000ms` + `jitter 0.25`), single worker
- **GraphQL** `https://www.threads.com/graphql/query` (`doc_id 37598244946487292`): token bucket `0.5 rps` + `429 Retry-After` + exponential backoff `base 2.0` (like `gallery-dl --sleep 3-6 --sleep-request 8-15 --sleep-429 120`)

Meta WAF checks JA3/JA4 TLS + HTTP/2 SETTINGS + `X-CSRFToken`/`X-ASBD-ID`/`X-IG-App-ID`. If `403` with `httpx`, install `curl_cffi` and use `--impersonate chrome` (byte-for-byte Chrome 131, same as `yt-dlp --impersonate chrome`).

## scrapmf Integration

`gallery-dl` has no Threads support — `scrapmf` routes Threads URLs to `threadstractormf` provider.

`~/.config/scrapmf/sites/threads.toml` (auto-generated, `0o600`):
```toml
site = "threads"
pattern = "threads.com"
patterns = ["threads.com", "threads.net"]
cookies_from_browser = "brave"
filename_template = "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}" # chronological, editable
directory_template = ["{scrapmf_root}", "{category}", "{username}", "{subcategory}"]
[rate_limit]
sleep = "3-6"
sleep_request = "8-15"
sleep_429 = 120
# Output: ~/scrapmf/<profile>/threads/<username>/posts/2026-04-14_EJEMPLO12345_01.jpg
```

Profile (`~/.config/scrapmf/profiles/<name>.toml`):
```toml
[[accounts.threads]]
username = "username"
# inherits cookies_from_browser brave; override per account if needed
```

Usage:
```bash
scrapmf scrape https://www.threads.com/@username/media
scrapmf scrape --cookies-from-browser brave https://www.threads.com/@user
# reorder filename like gallery-dl
# edit sites/threads.toml: filename_template = "{post_id}_{num:02d}.{extension}"
```

Archive is deferred — `threads` currently dedups by filename (deterministic `{date}_{post_id}`), not JSONL.

## Layout

```
threadstractormf/
  auth.py         # MozillaCookieJar + load_from_browser("brave")
  models.py       # Post/Media/Profile (pydantic)
  template.py     # {date:%Y-%m-%d}_{post_id}_{num:02d} renderer
  rate_limit.py   # BatchCooldownLimiter + ApiRateLimiter + backoff
  api.py          # GraphQL doc_id 37598244946487292 + Playwright DOM fallback
  downloader.py   # is_valid_media_url + download with rate-limit + template
  client.py       # Threadscraper facade
  cli.py          # Typer CLI
scripts/sniff.py  # Playwright capture of graphql (dev)
```

## Testing

```bash
pytest -q  # 13 tests: auth Netscape tabs, media_id, is_valid_media_url, BatchCooldown
```
