"""Template rendering for filenames — compatible with gallery-dl / scrapmf.

Supports variables: {post_id}, {media_id}, {username}, {category}, {subcategory},
{extension}, {date:%Y-%m-%d}, {num}, {num:02d}
and configurable ordering: "{date:%Y-%m-%d}_{post_id}_{num:02d}.{extension}" (chronological)
or "{post_id}_{num:02d}.{extension}" (post_id only).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from threadstractormf.models import sanitize_filename


def render_filename(
    template: str,
    *,
    post_id: str,
    media_id: str,
    username: str,
    category: str = "threads",
    subcategory: str = "posts",
    extension: str = "jpg",
    num: int = 1,
    date_iso: str | None = None,
) -> str:
    # parse date
    dt: datetime | None = None
    if date_iso:
        try:
            dt = datetime.fromisoformat(date_iso.replace("Z", "+00:00"))
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)

    # handle {date:FORMAT} and {date}
    def repl_date(m: re.Match) -> str:
        fmt = m.group(1)
        if fmt:
            # fmt like %Y-%m-%d or %Y
            try:
                return dt.strftime(fmt)
            except Exception:
                return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")

    # replace {date:%Y-%m-%d} or {date}
    template = re.sub(r"\{date(?::([^}]+))?\}", repl_date, template)

    # handle {num:02d} and {num}
    def repl_num(m: re.Match) -> str:
        fmt = m.group(1)
        if fmt:
            # support 02d, 2d, etc.
            try:
                # extract width
                width_match = re.search(r"0?(\d+)d", fmt)
                if width_match:
                    width = int(width_match.group(1))
                    return str(num).zfill(width)
            except Exception:
                pass
            return str(num)
        return str(num)

    template = re.sub(r"\{num(?::([^}]+))?\}", repl_num, template)

    # simple replacements
    replacements = {
        "{post_id}": post_id,
        "{media_id}": media_id,
        "{username}": username,
        "{category}": category,
        "{subcategory}": subcategory,
        "{extension}": extension.lstrip("."),
        "{ext}": extension.lstrip("."),
    }
    for k, v in replacements.items():
        template = template.replace(k, v)

    # handle {scrapmf_root} / {username} etc. already done
    return template


def render_directory(
    template_parts: list[str] | str,
    *,
    username: str,
    category: str = "threads",
    subcategory: str = "posts",
    scrapmf_root: str = "default",
) -> str:
    if isinstance(template_parts, str):
        parts = [template_parts]
    else:
        parts = template_parts
    # {username} comes from the CLI target, which the user types. It must be
    # reduced to a single harmless path component: otherwise
    # `--directory-template "{username}"` with the target `@..` resolves the
    # destination to the parent of --dest, and files land outside it silently.
    #
    # sanitize_filename is reused rather than a second filter being written, so
    # "untrusted text" has one definition in this package. It maps ".." and "."
    # to "", turns slashes into "_", and collapses runs of underscores.
    safe_user = sanitize_filename(username)
    out: list[str] = []
    for p in parts:
        # simple
        p = p.replace("{username}", safe_user)
        p = p.replace("{user}", safe_user)
        # {category}, {subcategory} and {scrapmf_root} are internal fixed values
        # or orchestrator-supplied, so they are left alone: sanitising them would
        # change the documented scrapmf layout for no gain.
        p = p.replace("{category}", category)
        p = p.replace("{subcategory}", subcategory)
        p = p.replace("{scrapmf_root}", scrapmf_root)
        # handle {scrapmf_root} legacy aliases
        p = p.replace("{scarpmf_root}", scrapmf_root)
        out.append(p)
    return "/".join(out)
