"""UTC storage and US Eastern display."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def utc_now() -> datetime:
    """Aware UTC timestamp."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Require an aware timestamp and convert it to UTC."""
    if value.tzinfo is None:
        msg = "timestamp must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(UTC)


def format_utc(value: datetime) -> str:
    """ISO-8601 UTC with an explicit offset."""
    return ensure_utc(value).isoformat(timespec="seconds")


def format_et(value: datetime) -> str:
    """Wall time in America/New_York, labeled ET."""
    local = ensure_utc(value).astimezone(ET)
    return local.strftime("%Y-%m-%d %H:%M:%S ET")


def parse_utc(text: str) -> datetime:
    """Parse an ISO timestamp written by `format_utc`."""
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def parse_duration(text: str) -> int:
    """Parse `30s`, `5m`, `24h`, or `1d` into seconds.

    A bare number is rejected so a unit cannot be assumed.
    """
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        msg = "duration must look like 30s, 5m, 24h, or 1d"
        raise ValueError(msg)
    amount = int(match.group(1))
    if amount <= 0:
        msg = "duration must be positive"
        raise ValueError(msg)
    return amount * _UNIT_SECONDS[match.group(2)]


def add_seconds(start: datetime, seconds: int) -> datetime:
    """UTC `start` plus a whole number of seconds."""
    return ensure_utc(start) + timedelta(seconds=seconds)
