# threadstractormf

Threads media downloader — `import threadstractormf` + CLI like `gallery-dl` / `yt-dlp`.

- **Auth** via Netscape `cookies.txt` (`MozillaCookieJar`) — same file as `gallery-dl --cookies` / `yt-dlp --cookies`
- **Media IDs** `post_id` (`/post/<code>`) + `_1/_2/_3` for carousels, no reposts
- **Media** photos, videos, profile pic (`t51.2885-19`)
- **Stack** `httpx[http2]` + `pydantic` + optional `curl_cffi` (TLS/JA4 impersonation) + `browser-cookie3` + `playwright` fallback, `uv`/`hatchling`, `requires-python >=3.10`
- **Pagination** GraphQL walks the `after` cursor, so `--limit` is honoured beyond the first page

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

### Output layout

With the default template, an account lands like this:

```
./dl/
├── photos/
│   ├── 2026-04-14_DPa1b2C3d4E5_01.jpg      single photo
│   ├── 2026-04-14_DPx9Y8z7W6V5_01.jpg      carousel → item 1 of 3
│   ├── 2026-04-14_DPx9Y8z7W6V5_03.jpg      carousel → item 3 of 3
│   └── 2026-04-14_DPcO2vE3rI4d_01.jpg      video poster (a JPEG)
├── videos/
│   ├── 2026-04-15_DPmN4oP5qR6S_01.mp4      video
│   └── 2026-04-18_DPx9Y8z7W6V5_02.mp4      carousel → the video item
├── profile/
│   └── usuario_profile.jpg                 avatar (no date; it has none)
└── .threadstractormf/
    └── dedup.jsonl                         download ledger
```

The `{num:02d}` counts media items within a carousel. The extension reflects the
real content (`video_cover.jpg` stays `.jpg`), and the folder comes from
`media.type` — the two are independent, so a `.webm` video goes to `videos/`.

**Splitting by type depends on the destination's name.** When `--dest` is a
plain directory, media is sorted into `photos/`/`videos/`/`profile/`. When it is
already one of those (or `posts/`, as `scrapmf` passes), everything stays flat:

| `--dest` | images | videos | avatar |
|---|---|---|---|
| `./dl` | `dl/photos/` | `dl/videos/` | `dl/profile/` |
| `./dl/posts` | `dl/posts/` | `dl/posts/` | `dl/posts/` |
| `./dl/photos` | `dl/photos/` | `dl/photos/` | `dl/photos/` |

### Download ledger (dedup)

Completed downloads are recorded in `<dest>/.threadstractormf/dedup.jsonl`, keyed
by **`(post_id, index)`** rather than by filename. That matters because the
filename embeds the date and the extension, and both can change — while the post
id is stable for the life of the post. Changing `--filename-template`, fixing an
extension guess, or a `{date}` that rolls over to today will **not** re-download
an existing archive.

A download is skipped when any of these hold:

1. `(post_id, index)` is in the ledger;
2. the exact destination file exists (and is then recorded);
3. a file for the same `post_id` under a different name is on disk — this adopts
   archives created by older versions, e.g. a `.webm` previously saved as `.jpg`.

The ledger is created on first use, so an existing archive keeps working
unchanged and is picked up incrementally. `--overwrite` bypasses it, and
`--no-archive` falls back to filename-only dedup.

Adoption matches on a delimiter after the id, so `DPxyz123` never adopts
`DPxyz1234` — a different post.

Note: adoption leaves files under their old name. Renaming them is a separate,
manual step.

### Options

```
target                          @username or https://www.threads.com/@user/media
-c, --cookies <path>            Netscape cookies.txt (gallery-dl compatible)
    --cookies-from-browser <str> brave|chrome|firefox|edge (reads local DB)
-d, --dest <path>               output dir [default: ./dl]
-l, --limit <int>               posts limit
    --profile-pic-only          only avatar
    --impersonate <str>         chrome (requires curl_cffi: pip install 'threadstractormf[antibot]')
    --overwrite
    --no-rate-limit             disable anti rate-limit (not recommended)
    --cooldown <int>            ms between downloads [default: 2000]
    --batch-cooldown <int>      ms each 100 downloads [default: 120000]
    --rps <float>               req/s for GraphQL API [default: 0.5]
    --filename-template <str>   e.g. "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}"
    --directory-template <str>  e.g. "{scrapmf_root}/{category}/{username}/{subcategory}"
    --no-archive                disable the download ledger (dedup by filename only)
    --no-adopt-existing         do not adopt files on disk under an older name
    --get-urls                  print the media URLs instead of downloading (dry run)
```

`--cookies` and `--cookies-from-browser` are alternatives, not combined: if both
are given the browser wins.

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

Meta WAF checks JA3/JA4 TLS + HTTP/2 SETTINGS + `X-CSRFToken`/`X-ASBD-ID`/`X-IG-App-ID`. If `403` with `httpx`, install `curl_cffi` and use `--impersonate chrome` (byte-for-byte Chrome, same mechanism as `yt-dlp --impersonate`). Accepted targets: `chrome`, `chrome131`, `firefox`, `safari`, `edge`, `tor` and other pinned fingerprints; an unknown name fails immediately with the list of valid ones.

Impersonation covers **both** the GraphQL calls and the CDN downloads — a WAF that blocks the API also blocks the media fetches, so impersonating only half the traffic would not help.

Retries only ever happen for genuinely transient failures: `429`, `5xx`, DNS/timeout/TLS resets. A permanent `403`/`404` is reported immediately instead of sitting through ~60s of backoff first.

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
  _backend.py     # httpx | curl_cffi backend, shared retry policy, pw cookies
  auth.py         # MozillaCookieJar + load_from_browser("brave")
  models.py       # Post/Media/Profile (pydantic)
  template.py     # {date:%Y-%m-%d}_{post_id}_{num:02d} renderer
  rate_limit.py   # BatchCooldownLimiter + ApiRateLimiter + backoff
  api.py          # GraphQL doc_id 37598244946487292 (paginated) + Playwright DOM fallback
  downloader.py   # is_valid_media_url + download with rate-limit + template
  client.py       # Threadscraper facade
  cli.py          # Typer CLI
scripts/sniff.py  # Playwright capture of graphql (dev)
```

## Testing

```bash
pytest -q  # 212 tests: auth, models, rate limiting (fixed + adaptive), retry policy
           # across both backends, GraphQL pagination, --impersonate, download
           # ledger and adoption, extension/quality extraction, user-id
           # resolution, subfolder layout, error messages
mypy threadstractormf scripts  # type check (also runs in CI)
```
