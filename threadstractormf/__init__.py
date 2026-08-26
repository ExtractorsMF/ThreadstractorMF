"""threadstractormf — Threads library (gallery-dl cookies + post_id naming)."""

from threadstractormf.models import Media, Post, Profile

try:
    from threadstractormf.client import Threadscraper  # lazy re-export
except Exception:  # pragma: no cover
    Threadscraper = None  # type: ignore[assignment]

__all__ = ["Threadscraper", "Post", "Media", "Profile"]
__version__ = "1.0.1"
