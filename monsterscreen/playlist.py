"""Turns the synced folder plus config into an ordered list of slides.

A slide is anything the player can show for a while. Media files are the
only source today; a news source can later produce slides of its own (for
example a rendered image) and be merged in with `interleave`.
"""

import datetime as dt
import random
from dataclasses import dataclass
from pathlib import Path

from .content_config import CONFIG_FILENAME, ContentConfig
from .transcode import needs_conversion

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".asf",
                    ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".3gp", ".flv"}


@dataclass(frozen=True)
class Slide:
    path: Path
    kind: str  # "image" or "video"
    duration: float  # seconds; for videos the maximum, 0 = until the end


def media_kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def media_files(media_dir: Path) -> list[Path]:
    if not media_dir.is_dir():
        return []
    return [p for p in media_dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and p.name.lower() != CONFIG_FILENAME
            and media_kind(p)]


def build(media_dir: Path, cfg: ContentConfig, today: dt.date, rng=random,
          converted=lambda path: None) -> list[Slide]:
    """`converted(path)` gives the playable conversion of a WMV, or None while
    it isn't ready; such videos are left out until then."""
    files = media_files(media_dir)

    if cfg.order == "newest":
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        files.sort(key=lambda p: p.name.lower())
        if cfg.order == "random":
            rng.shuffle(files)

    slides = []
    for path in files:
        rule = cfg.rule_for(path.name)
        if not rule.active_on(today):
            continue
        kind = media_kind(path)
        default = cfg.image_duration if kind == "image" else cfg.max_video_duration
        duration = rule.duration if rule.duration is not None else default
        if needs_conversion(path):
            path = converted(path)
            if path is None:
                continue
        slides.append(Slide(path, kind, duration))
    return slides


def interleave(slides: list[Slide], extra: list[Slide], every: int) -> list[Slide]:
    """Insert one `extra` slide after every `every` regular slides."""
    if not extra or every < 1:
        return list(slides)
    result, extra_index = [], 0
    for i, slide in enumerate(slides, start=1):
        result.append(slide)
        if i % every == 0:
            result.append(extra[extra_index % len(extra)])
            extra_index += 1
    return result
