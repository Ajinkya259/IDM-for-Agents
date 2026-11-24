"""
Times and speeds, read from the command line and said back the way people say them.

    --at 02:00, --at 2am, --at "2026-11-25 02:00"     a clock time (the next one, if no date)
    --in 30m, --in 1h30m, --in 2d                       a wait
    --limit 500K, --limit 2M, --limit 1.5MB             bytes a second (K = 1,000)

What can't be read raises ValueError, with words for the person who typed it.
"""
import datetime
import re
import time

CLOCK_FORMATS = ("%H:%M", "%H:%M:%S", "%I%p", "%I:%M%p", "%I %p", "%I:%M %p")
DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")
UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
SIZES = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}


def start_time(at=None, wait=None):
    """--at or --in as a time (seconds since the epoch), or None to start now."""
    if at and wait:
        raise ValueError("give --at or --in, not both")
    if wait:
        return time.time() + seconds_in(wait)
    return clock_time(at) if at else None


def seconds_in(text):
    """'1h30m' -> 5400."""
    parts = re.fullmatch(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text.strip().lower())
    if not parts or not any(parts.groups()):
        raise ValueError(f"--in takes a wait like 30m, 2h or 1h30m, not {text!r}")
    return sum(int(n) * unit for n, unit in zip(parts.groups(), UNITS.values()) if n)


def clock_time(text):
    """'02:00' -> the next 02:00 (today, or tomorrow if it has passed); a date and time as given."""
    text, now = text.strip().upper(), datetime.datetime.now()
    for fmt in DATE_FORMATS:
        try:
            when = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if when <= now:
            raise ValueError(f"--at {text} has already passed")
        return when.timestamp()
    for fmt in CLOCK_FORMATS:
        try:
            t = datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        when = datetime.datetime.combine(now.date(), t)
        return (when if when > now else when + datetime.timedelta(days=1)).timestamp()
    raise ValueError(f"--at takes a time like 02:00, 2am or '2026-11-25 02:00', not {text!r}")


def rate(text):
    """'2M' -> 2000000 bytes a second.  '0', 'none' and 'off' mean no limit (None)."""
    text = text.strip().lower()
    if text in ("0", "none", "off"):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg]?)(?:b|b/s|/s)?", text)
    if not m or float(m.group(1)) * SIZES[m.group(2)] < 1000:
        raise ValueError(f"a speed limit looks like 500K or 2M (at least 1K), not {text!r}")
    return int(float(m.group(1)) * SIZES[m.group(2)])


def clock(t):
    """A time as people say it: '14:30', '02:00 tomorrow', 'Nov 27 02:00'."""
    when, today = datetime.datetime.fromtimestamp(t), datetime.date.today()
    if when.date() == today:
        return when.strftime("%H:%M")
    if when.date() == today + datetime.timedelta(days=1):
        return when.strftime("%H:%M tomorrow")
    return when.strftime("%b %d %H:%M")


def iso(t):
    """A time for an agent: '2026-11-25T02:00:00+05:30'."""
    return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def how_long(seconds):
    """5400 -> '1h 30m'; 90 -> '1m 30s' (the two biggest units)."""
    left, words = max(0, int(seconds)), []
    for name, unit in UNITS.items():
        n, left = divmod(left, unit)
        if n:
            words.append(f"{n}{name}")
    return " ".join(words[:2]) or "0s"
