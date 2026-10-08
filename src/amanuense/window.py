"""Time windows: which mail a run covers."""
import re
from datetime import datetime, timedelta
from pathlib import Path

HELP = """24h, 3d, 2w (the last N hours/days/weeks), today, yesterday, week (since Monday),
last (since the previous successful run, or 24h the first time),
2026-10-01 (since that day), 2026-10-01..2026-10-05 (those days, inclusive)"""

_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


def _day(s, tz):
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        raise ValueError(f"bad date {s!r}, expected YYYY-MM-DD") from None


def parse_window(spec, now=None, last_run=None):
    """Return (start, end) as aware datetimes; end is exclusive."""
    now = now or datetime.now().astimezone()
    tz = now.tzinfo
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s = spec.strip().lower()
    if m := re.fullmatch(r"(\d+)\s*([hdw])", s):
        return now - timedelta(**{_UNITS[m[2]]: int(m[1])}), now
    if s == "today":
        return midnight, now
    if s == "yesterday":
        return midnight - timedelta(days=1), midnight
    if s == "week":
        return midnight - timedelta(days=midnight.weekday()), now
    if s == "last":
        return (last_run or now - timedelta(hours=24)), now
    if ".." in s:
        a, b = s.split("..", 1)
        start = _day(a, tz) if a else now - timedelta(hours=24)
        end = _day(b, tz) + timedelta(days=1) if b else now
        if end <= start:
            raise ValueError(f"empty window {spec!r}")
        return start, end
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return _day(s, tz), now
    raise ValueError(f"unknown window {spec!r}; use one of: {HELP}")


def describe(start, end):
    fmt = "%a %d %b %H:%M"
    return f"{start:{fmt}} → {end:{fmt}}"


def state_file(command):
    return Path.home() / ".local" / "state" / "amanuense" / f"last-{command}"


def read_last_run(command):
    try:
        return datetime.fromisoformat(state_file(command).read_text().strip())
    except (OSError, ValueError):
        return None


def write_last_run(command, end):
    p = state_file(command)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(end.isoformat() + "\n")
