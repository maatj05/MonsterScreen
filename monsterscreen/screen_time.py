"""When the screen is on.

The weekly schedule set on the setup page applies; without one, the
[schedule] in config.toml does. On top of that the setup page can set a
temporary override: "off until the next scheduled start" or "on until 03:00".
"""

import datetime as dt
import re
from dataclasses import dataclass

from .content_config import WEEKDAYS

DAY_NAMES = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
SHORT_DAY_NAMES = ("ma", "di", "wo", "do", "vr", "za", "zo")
MINUTE = dt.timedelta(minutes=1)
LOOKAHEAD = dt.timedelta(days=8)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(text: str) -> int:
    """"08:30" -> minutes after midnight."""
    m = _TIME_RE.match(text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"Ongeldige tijd: {text!r} (gebruik uu:mm)")
    return int(m.group(1)) * 60 + int(m.group(2))


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class WeekSchedule:
    """One on-period per weekday. An end at or before the start runs on past
    midnight into the next day, so 18:00-02:00 and 00:00-00:00 (all day) work."""

    days: dict[str, tuple[int, int] | None]  # weekday -> (start, end), None = off all day
    enabled: bool = True

    @classmethod
    def from_state(cls, data: dict | None) -> "WeekSchedule | None":
        if not data:
            return None
        days = {}
        for day in WEEKDAYS:
            value = data.get(day, "off")
            if value == "off":
                days[day] = None
            else:
                start, end = value.split("-")
                days[day] = (parse_time(start), parse_time(end))
        return cls(days, bool(data.get("enabled", True)))

    def to_state(self) -> dict:
        data = {"enabled": self.enabled}
        for day in WEEKDAYS:
            period = self.days.get(day)
            data[day] = f"{format_time(period[0])}-{format_time(period[1])}" if period else "off"
        return data

    def is_on(self, now: dt.datetime) -> bool:
        minute = now.hour * 60 + now.minute
        today = self.days.get(WEEKDAYS[now.weekday()])
        if today:
            start, end = today
            if start <= minute < (end if end > start else 24 * 60):
                return True
        yesterday = self.days.get(WEEKDAYS[(now.weekday() - 1) % 7])
        if yesterday:
            start, end = yesterday
            if end <= start and minute < end:
                return True
        return False


@dataclass
class Override:
    on: bool
    until: dt.datetime | None  # None: until changed on the setup page

    @classmethod
    def from_state(cls, data: dict | None) -> "Override | None":
        if not data:
            return None
        until = dt.datetime.fromisoformat(data["until"]) if data.get("until") else None
        return cls(bool(data["on"]), until)

    def to_state(self) -> dict:
        return {"on": self.on, "until": self.until.isoformat() if self.until else None}

    def active(self, now: dt.datetime) -> bool:
        return self.until is None or now < self.until


def base_schedule(week_schedule: dict | None, config_schedule):
    """The schedule from the setup page when it is switched on, else config.toml's."""
    week = WeekSchedule.from_state(week_schedule)
    return week if week and week.enabled else config_schedule


def is_on(schedule, override: Override | None, now: dt.datetime) -> bool:
    if override and override.active(now):
        return override.on
    return schedule.is_on(now)


def _minutes_ahead(now: dt.datetime):
    t = now.replace(second=0, microsecond=0)
    while t - now < LOOKAHEAD:
        t += MINUTE
        yield t


def next_start(schedule, now: dt.datetime) -> dt.datetime | None:
    """The next moment the schedule switches the screen on, if any."""
    for t in _minutes_ahead(now):
        if schedule.is_on(t) and not schedule.is_on(t - MINUTE):
            return t
    return None


def next_change(schedule, now: dt.datetime) -> dt.datetime | None:
    current = schedule.is_on(now)
    return next((t for t in _minutes_ahead(now) if schedule.is_on(t) != current), None)


def next_time_of_day(now: dt.datetime, text: str) -> dt.datetime:
    """"03:00" -> the first 03:00 after now (tonight, or tomorrow when already past)."""
    minutes = parse_time(text)
    t = now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def describe_moment(t: dt.datetime, now: dt.datetime) -> str:
    days = (t.date() - now.date()).days
    day = "" if days == 0 else "morgen " if days == 1 else SHORT_DAY_NAMES[t.weekday()] + " "
    return f"{day}{t:%H:%M}"


def describe(schedule, override: Override | None, now: dt.datetime) -> str:
    """"Aan, volgens schema tot 18:00", "Uit tot morgen 08:00", ..."""
    if override and override.active(now):
        state = "Aan" if override.on else "Uit"
        if override.until is None:
            return f"{state} tot je het weer verandert"
        return f"{state} tot {describe_moment(override.until, now)}"
    state = "Aan" if schedule.is_on(now) else "Uit"
    change = next_change(schedule, now)
    return f"{state}, volgens schema" + (f" tot {describe_moment(change, now)}" if change else "")
