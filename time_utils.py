"""Explicit time semantics; avoid guessing offsets for ambiguous floating times."""
from datetime import datetime, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import JMAPError

INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$")


def instant(value):
    try:
        if not isinstance(value, str) or not INSTANT.fullmatch(value):
            raise ValueError
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        raise JMAPError("invalid_time", "Use an RFC3339 instant with an explicit offset.") from None


def zone(value):
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise JMAPError("invalid_timezone", "Use a valid IANA timezone.") from None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local(value, timezone_name):
    try:
        if not isinstance(value, str) or not LOCAL.fullmatch(value):
            raise ValueError
        naive = datetime.fromisoformat(value)
    except ValueError:
        raise JMAPError("invalid_time", "Event start must be a local datetime without an offset.") from None
    tz = zone(timezone_name)
    candidates = {naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
                  if naive.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == naive}
    if len(candidates) != 1:
        raise JMAPError("ambiguous_time", "The wall time is ambiguous or nonexistent in this timezone; choose an unambiguous time.")
    return candidates.pop()


def query_range(start, end, timezone_name):
    first, last = instant(start), instant(end)
    if last <= first:
        raise JMAPError("invalid_range", "The end must be after the start.")
    tz = zone(timezone_name)
    # JMAP calendar filters are LocalDateTime interpreted using the query timeZone.
    boundaries = [dt.astimezone(tz).replace(tzinfo=None).isoformat() for dt in (first, last)]
    for value in boundaries:
        local(value, timezone_name)
    return first, last, boundaries
