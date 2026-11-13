# threadstractormf

Threads media downloader — Python library + CLI, gallery-dl/yt-dlp compatible cookies.

## Project memory

- Index: this README + `docs/` + `docs/memory/`.
- Compact memory: `docs/memory/architecture.md`, `docs/memory/decisions.md`, `docs/memory/state.md`, `docs/memory/conventions.md`, `docs/memory/testing.md`.
- Detailed docs: `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `docs/SECURITY.md`, `docs/PRIVACY.md`, `docs/GIT_WORKFLOW.md`, `docs/DECISIONS.md`.
- LLM indexes: `llms.txt`, `llms-full.txt`.
- Agent instructions: `../AGENTS.md` (workspace root).
- Last documentation review: 2026-11-03.

## Stack

Python ≥ 3.10, `httpx[http2]`, `pydantic`, `typer`, `rich`. Optional: `curl_cffi` (antibot), `playwright` + `browser-cookie3` (browser).

## Install

```bash
uv pip install -e ".[dev]"          # dev
uv pip install -e ".[antibot]"      # TLS impersonation
uv pip install -e ".[browser]"      # Playwright + cookies
```

## Quickstart

Library:

```python
from threadstractormf import Threadscraper
scraper = Threadscraper(cookies="cookies.txt")
posts = scraper.get_posts("user", limit=20)
for post in posts:
    for media in post.media:
        scraper.download(media, dest="./dl", username=post.username, date_iso=post.datetime_iso)
scraper.download_profile_pic("user", dest="./dl")
```

CLI:

```bash
threadstractormf --cookies cookies.txt --dest ./dl @user
threadstractormf --cookies-from-browser brave --dest ./dl @user
threadstractormf --cookies cookies.txt --limit 50 --dest ./dl @user
threadstractormf --cookies cookies.txt --get-urls --dest ./dl @user
threadstractormf --version
```

## Modules

| Module | Responsibility |
|---|---|
| client.py | Facade, wiring, download orchestration |
| api.py | GraphQL walk + Playwright DOM fallback |
| _discovery.py | Capture/cache GraphQL template |
| _backend.py | httpx/curl_cffi backends |
| auth.py | Netscape/browser cookies |
| models.py | Post/Media/Profile |
| template.py | Filename/directory rendering |
| rate_limit.py | Cooldowns + backoff |
| archive.py | Download ledger |
| downloader.py | CDN download + adoption |
| cli.py | Typer CLI |

## Docker

Published to GitHub Container Registry (GHCR) on every push to `main`:

```bash
# slim (~150 MB): core only, fast GraphQL path
docker pull ghcr.io/extractorsmf/threadstractormf:slim

# full (~1 GB): slim + antibot (curl_cffi) + browser (Playwright)
docker pull ghcr.io/extractorsmf/threadstractormf:full   # also tagged :latest
docker pull ghcr.io/extractorsmf/threadstractormf:1.2.0
```

Run (mount cookies and destination as volumes; never bake secrets into the image):

```bash
docker run --rm \\
  -v $(pwd)/cookies.txt:/cookies.txt:ro \\
  -v $(pwd)/dl:/dl \\
  ghcr.io/extractorsmf/threadstractormf:full \\
  --cookies /cookies.txt --dest /dl @user
```

- `THREADS_USE_SANDBOX=1` is set in the `full` image so Playwright works unprivileged.
- Multi-arch: `linux/amd64`, `linux/arm64`.

## Install from PyPI

```bash
pip install threadstractormf
pip install 'threadstractormf[browser]'
pip install 'threadstractormf[antibot]'
```

## Essential commands

```bash
uv run pytest -q
uv run ruff check .
uv run mypy threadstractormf scripts
```

## Key constraints

- Ledger opt-in (`--archive`); keyed by `(post_id, index)`.
- Browser fallback runs anonymously.
- GraphQL doc_id is discovered, not hardcoded.
- stdout = one line per downloaded file; diagnostics on stderr.
- Do not commit secrets or personal identities.

## More details

- Usage/CLI details: `docs/DEVELOPMENT.md` (commands), `docs/ARCHITECTURE.md` (flow).
- Security/privacy: `docs/SECURITY.md`, `docs/PRIVACY.md`.
- Git/commits: `docs/GIT_WORKFLOW.md`.
- Decisions: `docs/DECISIONS.md`.
- Known issues/state: `docs/memory/state.md`.

## Status

408 tests pass; ruff and mypy clean. Known gaps: duplicate summary line, library `archive` default, relay flag override, profile-pic ext derivation. See `docs/memory/state.md`.
