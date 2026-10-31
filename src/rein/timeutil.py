"""Timestamp helpers. Internally everything is UTC; Graph API returns e.g. 2026-10-27T14:05:00+0900."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_COMPACT_OFFSET = re.compile(r"^(.*\d)([+-]\d{2})(\d{2})$")


def parse_time(value: str | int | float | datetime) -> datetime:
    """Parse ISO 8601 (with `Z`, `+09:00` or `+0900`) or a unix timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        s = value.strip()
        if s.isdigit():
            return datetime.fromtimestamp(int(s), tz=timezone.utc)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        m = _COMPACT_OFFSET.match(s)
        if m and "T" in s:
            s = f"{m.group(1)}{m.group(2)}:{m.group(3)}"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
