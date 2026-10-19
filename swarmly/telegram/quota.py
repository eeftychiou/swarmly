"""When a provider says its quota comes back, read from the error text of a failed run.

A quota freeze (#49.E) that records this time lifts itself then; one without it is retried
after ``limits.quota_retry_minutes``. The CLIs say it in a few ways (live examples):

- Codex: ``... or try again at Nov 16th, 2026 10:06 PM.``
- Gemini / Antigravity: ``Quota exceeded ... Please retry in 41.2s.`` or ``resets in 3h``
- HTTP-style: ``Retry-After: 120``
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

#: A reset further out than this is not believed (a misread date); the retry interval applies.
MAX_WAIT_SECONDS = 45 * 24 * 3600.0

_UNITS = {"s": 1.0, "sec": 1.0, "second": 1.0, "m": 60.0, "min": 60.0, "minute": 60.0,
          "h": 3600.0, "hr": 3600.0, "hour": 3600.0, "d": 86400.0, "day": 86400.0}
_IN = re.compile(r"\b(?:retry|try again|reset|resets|available again|again)\s+(?:in|after)\s+"
                 r"(\d+(?:\.\d+)?)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)\b",
                 re.IGNORECASE)
_RETRY_AFTER = re.compile(r"\bretry[- ]after[:=\s]+(\d+)\b", re.IGNORECASE)
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_AT = re.compile(r"\b(?:try again|retry|resets?|available again)\s+(?:at|on)\s+"
                 r"(?:(?P<month>[A-Za-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+"
                 r"(?:(?P<year>\d{4}),?\s+)?(?:at\s+)?)?"
                 r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>[AaPp]\.?[Mm]\.?)?",
                 re.IGNORECASE)


def quota_resets_at(text: str, now: float) -> Optional[float]:
    """The epoch time the quota comes back, or None when the text does not say (or says
    something implausible). A clock time with no zone is read as this machine's local time,
    as the CLI that printed it runs here."""
    text = text or ""
    match = _IN.search(text)
    if match:
        unit = match.group(2).lower().rstrip("s") or "s"
        seconds = float(match.group(1)) * _UNITS.get(unit, _UNITS.get(unit[:1], 1.0))
        return _plausible(now + seconds, now)
    match = _RETRY_AFTER.search(text)
    if match:
        return _plausible(now + float(match.group(1)), now)
    match = _AT.search(text)
    if match:
        return _plausible(_clock_time(match, now), now)
    return None


def _clock_time(match: "re.Match[str]", now: float) -> Optional[float]:
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    today = dt.datetime.fromtimestamp(now)
    try:
        if match.group("month"):
            month = _MONTHS.get(match.group("month")[:3].lower())
            if month is None:
                return None
            year = int(match.group("year") or today.year)
            when = dt.datetime(year, month, int(match.group("day")), hour, minute)
            if not match.group("year") and when.timestamp() < now - 86400:
                when = when.replace(year=year + 1)         # "Jan 2" read in December
        else:
            when = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if when.timestamp() <= now:
                when += dt.timedelta(days=1)
    except ValueError:
        return None
    return when.timestamp()


def _plausible(when: Optional[float], now: float) -> Optional[float]:
    if when is None or when <= now or when - now > MAX_WAIT_SECONDS:
        return None
    return when


def ttl_for(until: Optional[float], now: float) -> Optional[str]:
    """An envelope ``ttl`` (whole minutes) for a freeze lasting until ``until``."""
    if until is None or until <= now:
        return None
    return f"{max(1, int((until - now + 59) // 60))}m"


def describe_until(until: Optional[float]) -> str:
    """'until 16 Nov 22:06' in this machine's local time, or '' when there is no end."""
    if until is None:
        return ""
    return "until " + dt.datetime.fromtimestamp(until).strftime("%d %b %H:%M").lstrip("0")
