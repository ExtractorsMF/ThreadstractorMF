"""Download ledger — remember what has already been fetched, by post id.

Why this exists
---------------
The only deduplication this tool used to have was ``if out.exists()``: the
*filename* was the key. That is fragile, because the filename is assembled from
three pieces and only one of them is stable::

    {date}__{post_id}__{num}.{extension}
     unstable  STABLE     unstable  unstable

* ``{extension}`` is derived from the URL, and a wrong guess silently renamed
  whole categories of media between releases (a ``.webm`` saved as ``.jpg``, a
  JPEG poster saved as ``.mp4``). Every such fix re-downloaded the user's entire
  history.
* ``{date}`` falls back to *today* when the post date is unknown, so the very
  same post produced a different filename on the next day.

``post_id`` and ``index`` come from the API and do not change while the post
exists, so they make a dependable key. The ledger records them and the
downloader consults it before touching the network.

Storage
-------
Append-only JSONL, one object per completed download::

    {"post_id": "DPxyz", "index": 1, "path": "...", "t": 1756000000}

The shape mirrors scrapmf's own archive
(``~/.config/scrapmf/archive/<site>/<account>.jsonl``), so the two can be
unified later without a migration. The file lives inside the media directory
(``<dest>/.threadstractormf/dedup.jsonl``) so that moving the library moves the
record along with it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

# Hidden directory kept beside the media so it travels with an archive.
LEDGER_DIRNAME = ".threadstractormf"
LEDGER_FILENAME = "dedup.jsonl"


def ledger_path_for(dest: str | Path) -> Path:
    """Path of the ledger that covers ``dest``.

    ``dest`` must be the *root* destination, not a ``photos/``/``videos/``
    subdirectory, otherwise one account ends up with several ledgers.
    """
    return Path(dest).expanduser().resolve() / LEDGER_DIRNAME / LEDGER_FILENAME


class DownloadLedger:
    """Append-only set of ``(post_id, index)`` pairs already on disk.

    Loading is eager but cheap: the file holds one short line per media item, so
    even a few thousand posts parse in milliseconds. Writes are appended
    immediately so an interrupted run still records what it finished.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._keys: set[tuple[str, int]] = set()

    # ── reading ─────────────────────────────────────────────────────────────

    def load(self) -> None:
        """Read the ledger into memory. Missing/corrupt lines are skipped."""
        self._keys = set()
        if self.path is None or not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    key = _entry_key(entry)
                except Exception:
                    continue  # a torn or hand-edited line must not break a scrape
                if key is not None:
                    self._keys.add(key)

    def has(self, post_id: str, index: int) -> bool:
        return (post_id, index) in self._keys

    def __len__(self) -> int:
        return len(self._keys)

    # ── writing ─────────────────────────────────────────────────────────────

    def record(self, post_id: str, index: int, path: str | Path) -> None:
        """Mark ``(post_id, index)`` as present on disk."""
        if self.path is None:
            return
        key = (post_id, index)
        if key in self._keys:
            return
        self._keys.add(key)
        entry = {
            "post_id": post_id,
            "index": index,
            "path": str(path),
            "t": int(time.time()),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(self.path, 0o600)
        except OSError:
            # An unwritable ledger must never abort a download: worst case the
            # filename check keeps deduping as it always did.
            pass


def _entry_key(entry: Any) -> tuple[str, int] | None:
    if not isinstance(entry, dict):
        return None
    post_id = entry.get("post_id")
    if not isinstance(post_id, str) or not post_id:
        return None
    index = entry.get("index", 1)
    if not isinstance(index, int):
        return None
    return (post_id, index)


# ── legacy-file adoption ───────────────────────────────────────────────────
#
# When the extension (or any other part of the name) starts being computed
# differently, files already on disk keep the *old* name. The ledger starts
# empty at that point, so the plain existence check misses them. Matching on
# the post id recovers them instead of downloading everything again.


def find_existing_for_post(dest: str | Path, post_id: str, index: int = 1) -> Path | None:
    """Find an already-downloaded file for item ``index`` of ``post_id``.

    Two things must line up, or carousel items overwrite each other's history:

    * a delimiter after the id — end of name, ``.`` or ``_`` — so ``DPxyz123``
      never adopts ``DPxyz1234``, which is a different post;
    * the item number that follows, when the filename carries one. Every item of
      a carousel shares the same ``post_id``; only ``{num}`` distinguishes them.
      Without this check item 2 of a carousel adopts item 1's file and is never
      downloaded at all.

    A file with no number can only belong to item 1: that is a post whose media
    is not part of a carousel, or the ``{post_id}.{ext}`` template.
    """
    directory = Path(dest)
    if not directory.is_dir():
        return None

    numbered: Path | None = None
    plain: Path | None = None
    for candidate in sorted(directory.iterdir()):
        if not candidate.is_file():
            continue
        name = candidate.name
        position = name.find(post_id)
        if position == -1:
            continue
        tail = name[position + len(post_id) :]
        if tail[:1] not in ("", ".", "_"):
            continue
        match = re.match(r"^_(\d+)", tail)
        if match:
            if int(match.group(1)) == index and numbered is None:
                numbered = candidate
        elif index == 1 and plain is None:
            plain = candidate

    # An explicitly numbered file is what the current naming scheme produces, so
    # it wins over a bare "{post_id}.{ext}" that also matches item 1.
    return numbered or plain
