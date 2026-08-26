"""Pydantic models — map the extension post_metadata but with post_id naming.

Extension (content.js:678-695):
  username, datetime_iso, datetime_display, post_permalink, media_urls,
  post_content, like_count, reply_count
Library:
  Post.id = post_id extracted from permalink /post/<ID> (regex content.js:311)
  Media.id = post_id if single media, or f"{post_id}_{i}" for carousels (user spec)
  Profile.profile_pic with id f"{username}_profile"
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MediaType = Literal["image", "video"]


class Media(BaseModel):
    id: str = Field(description="media_id: post_id or post_id_N for carousels")
    post_id: str = Field(description="base post_id (/post/<ID>)")
    index: int = Field(ge=1, description="1-index within the carousel")
    type: MediaType
    url: str
    ext: str = Field(description="jpg|mp4|webp... derived from URL")
    width: int | None = None
    height: int | None = None

    @field_validator("ext")
    @classmethod
    def normalize_ext(cls, v: str) -> str:
        return v.lower().lstrip(".")

    def filename(
        self,
        *,
        prefix: str | None = None,
        template: str | None = None,
        username: str | None = None,
        category: str = "threads",
        subcategory: str = "posts",
        date_iso: str | None = None,
    ) -> str:
        """Generate filename. If template is None uses simple post_id naming,
        otherwise gallery-dl style template."""
        if template:
            from threadstractormf.template import render_filename

            return render_filename(
                template,
                post_id=self.post_id,
                media_id=self.id,
                username=username or self.post_id,
                category=category,
                subcategory=subcategory,
                extension=self.ext,
                num=self.index,
                date_iso=date_iso,
            )
        # For profile pics, id is already username_profile
        if self.post_id.endswith("_profile"):
            return f"{self.id}.{self.ext}"
        # Detect carousel by id != post_id
        if self.id != self.post_id:
            return f"{self.id}.{self.ext}"
        return f"{self.post_id}.{self.ext}"


class Post(BaseModel):
    id: str = Field(description="post_id = short code from /post/<ID>")
    permalink: str
    username: str
    caption: str | None = None
    datetime_iso: str | None = None
    datetime_display: str | None = None
    like_count: int = 0
    reply_count: int = 0
    media: list[Media] = Field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return len(self.media) > 1

    def primary_media(self) -> Media | None:
        return self.media[0] if self.media else None


class Profile(BaseModel):
    username: str
    user_id: str | None = None
    full_name: str | None = None
    bio: str | None = None
    profile_pic_url: str | None = None
    profile_pic_media: Media | None = None
    is_private: bool = False
    follower_count: int | None = None


# Helpers portados de background.js
def extract_post_id(permalink: str) -> str | None:
    """Extract post_id from permalink. Replicates content.js:311 regex /post/([^/]+)."""
    import re

    m = re.search(r"/post/([^/?#]+)", permalink)
    return m.group(1) if m else None


def sanitize_filename(name: str) -> str:
    """Port of background.js:147-154 sanitizeFilename."""
    import re

    s = re.sub(r"[\/\\\?\*\|<>:\"\"]", "_", name)
    s = s.replace("..", "_")
    s = s.lstrip(".")
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    return s[:100]


def format_datetime_to_filename(iso: str | None) -> str | None:
    """Port of background.js:21-38 formatDatetime (YYYY-MM-DD_HH-M-S).
    Used for metadata only, not for post_id filenames."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (
            f"{dt.year}-{dt.month:02d}-{dt.day:02d}_"
            f"{dt.hour:02d}-{dt.minute:02d}-{dt.second:02d}"
        )
    except Exception:
        return None
