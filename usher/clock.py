"""The server's local time: the container's time zone (TZ, an IANA name such as Europe/Paris), UTC when unset."""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def valid(name):
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def zone():
    """The zone named by TZ, followed through clock changes; UTC when TZ is unset or not a zone name (the start-up
    check says so in the log)."""
    name = os.environ.get("TZ", "").strip()
    return ZoneInfo(name) if name and valid(name) else timezone.utc


def to_local(dt):
    return dt.astimezone(zone())


def today():
    return datetime.now(zone()).date()
