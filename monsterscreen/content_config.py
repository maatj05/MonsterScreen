"""The config.toml that lives in the Dropbox folder next to the media.

Everything in here can be changed by whoever manages the Dropbox folder.
A broken file never blanks the screen: we log the problem and keep using
the last config that did load.
"""

import datetime as dt
import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_FILENAME = "config.toml"

ORDERS = ("name", "newest", "random")
SCALINGS = ("fit", "fill")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_RANGE_RE = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


class ConfigError(ValueError):
    pass


@dataclass
class FileRule:
    duration: float | None = None
    skip: bool = False
    start_date: dt.date | None = None
    end_date: dt.date | None = None

    def active_on(self, day: dt.date) -> bool:
        if self.skip:
            return False
        if self.start_date and day < self.start_date:
            return False
        if self.end_date and day > self.end_date:
            return False
        return True


@dataclass
class Schedule:
    """On/off times. A day maps to a list of (start, end) minute ranges."""

    enabled: bool = False
    days: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    dates: dict[dt.date, list[tuple[int, int]]] = field(default_factory=dict)

    def is_on(self, now: dt.datetime) -> bool:
        if not self.enabled:
            return True
        ranges = self.dates.get(now.date())
        if ranges is None:
            ranges = self.days.get(WEEKDAYS[now.weekday()])
        if ranges is None:
            return True  # day not mentioned: screen stays on
        minute = now.hour * 60 + now.minute
        return any(start <= minute < end for start, end in ranges)


@dataclass
class NewsConfig:
    """Placeholder for a later version that shows news from an external API."""

    enabled: bool = False
    url: str = ""
    every: int = 5  # show a news slide after this many media items
    duration: float = 15


@dataclass
class ContentConfig:
    image_duration: float = 10
    order: str = "name"
    video_sound: bool = False
    max_video_duration: float = 0  # 0 = play the whole video
    scaling: str = "fit"
    rotate: int = 0
    sync_interval: int = 300
    files: dict[str, FileRule] = field(default_factory=dict)
    schedule: Schedule = field(default_factory=Schedule)
    news: NewsConfig = field(default_factory=NewsConfig)

    def rule_for(self, filename: str) -> FileRule:
        return self.files.get(filename.lower(), FileRule())


def parse_ranges(value) -> list[tuple[int, int]]:
    """Parse "on", "off" or "07:30-12:00, 13:00-18:00" into minute ranges."""
    if value is True:
        return [(0, 24 * 60)]
    if value is False:
        return []
    if not isinstance(value, str):
        raise ConfigError(f"ongeldige tijden: {value!r}")
    text = value.strip().lower()
    if text in ("aan", "on"):
        return [(0, 24 * 60)]
    if text in ("uit", "off"):
        return []
    ranges = []
    for part in text.split(","):
        match = _RANGE_RE.match(part.strip())
        if not match:
            raise ConfigError(f"ongeldige tijden: {value!r} (verwacht bv. \"07:30-18:00\")")
        h1, m1, h2, m2 = map(int, match.groups())
        start, end = h1 * 60 + m1, h2 * 60 + m2
        if m1 > 59 or m2 > 59 or start > 24 * 60 or end > 24 * 60 or end <= start:
            raise ConfigError(f"ongeldige tijden: {part.strip()!r}")
        ranges.append((start, end))
    return ranges


def _number(section: dict, key: str, default, minimum=0):
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} moet een getal >= {minimum} zijn, niet {value!r}")
    return value


def _choice(section: dict, key: str, default: str, choices: tuple[str, ...]) -> str:
    value = section.get(key, default)
    if value not in choices:
        raise ConfigError(f"{key} moet een van {', '.join(choices)} zijn, niet {value!r}")
    return value


def _date(value, key: str) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value)
        except ValueError:
            pass
    raise ConfigError(f"{key} moet een datum zijn zoals 2026-12-01, niet {value!r}")


def parse(data: dict) -> ContentConfig:
    cfg = ContentConfig()

    display = data.get("display", {})
    cfg.image_duration = _number(display, "image_duration", cfg.image_duration, minimum=1)
    cfg.order = _choice(display, "order", cfg.order, ORDERS)
    cfg.video_sound = bool(display.get("video_sound", cfg.video_sound))
    cfg.max_video_duration = _number(display, "max_video_duration", cfg.max_video_duration)
    cfg.scaling = _choice(display, "scaling", cfg.scaling, SCALINGS)
    cfg.rotate = _number(display, "rotate", cfg.rotate)
    if cfg.rotate not in (0, 90, 180, 270):
        raise ConfigError(f"rotate moet 0, 90, 180 of 270 zijn, niet {cfg.rotate!r}")

    sync = data.get("sync", {})
    cfg.sync_interval = _number(sync, "interval", cfg.sync_interval, minimum=30)

    for name, rule in data.get("files", {}).items():
        if not isinstance(rule, dict):
            raise ConfigError(f"[files.\"{name}\"] moet een sectie zijn")
        cfg.files[name.lower()] = FileRule(
            duration=_number(rule, "duration", None, minimum=1) if "duration" in rule else None,
            skip=bool(rule.get("skip", False)),
            start_date=_date(rule.get("from"), "from"),
            end_date=_date(rule.get("until"), "until"),
        )

    schedule = data.get("schedule", {})
    cfg.schedule.enabled = bool(schedule.get("enabled", False))
    for key, value in schedule.items():
        if key in ("enabled", "dates"):
            continue
        if key not in WEEKDAYS:
            raise ConfigError(f"onbekende dag in [schedule]: {key!r} (gebruik {', '.join(WEEKDAYS)})")
        cfg.schedule.days[key] = parse_ranges(value)
    for key, value in schedule.get("dates", {}).items():
        cfg.schedule.dates[_date(key, "datum in [schedule.dates]")] = parse_ranges(value)

    news = data.get("news", {})
    cfg.news = NewsConfig(
        enabled=bool(news.get("enabled", False)),
        url=str(news.get("url", "")),
        every=int(_number(news, "every", 5, minimum=1)),
        duration=_number(news, "duration", 15, minimum=1),
    )
    return cfg


class ContentConfigLoader:
    """Reloads config.toml when it changes; keeps the last good version on errors."""

    def __init__(self, path: Path):
        self.path = path
        self.current = ContentConfig()
        self._loaded_mtime: float | None = None

    def load(self) -> ContentConfig:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            if self._loaded_mtime is not None:
                log.info("%s removed, using defaults", self.path.name)
                self.current, self._loaded_mtime = ContentConfig(), None
            return self.current
        if mtime == self._loaded_mtime:
            return self.current
        self._loaded_mtime = mtime
        try:
            with self.path.open("rb") as f:
                self.current = parse(tomllib.load(f))
            log.info("Loaded %s", self.path.name)
        except (tomllib.TOMLDecodeError, ConfigError) as e:
            log.error("Error in %s, keeping previous settings: %s", self.path.name, e)
        return self.current
