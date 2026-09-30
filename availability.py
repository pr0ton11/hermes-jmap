"""Busy interval union from fully scanned, server-expanded calendar instances."""
from . import models
from .errors import JMAPError
from .time_utils import instant, iso, query_range

MAX_PAGES = 100


def calendar_availability(calendar, start, end, calendar_ids=None, timezone=None, include_free=True):
    timezone = timezone or calendar.client.config.timezone
    first, last, _ = query_range(start, end, timezone)
    periods = []
    states, query_state, position = set(), None, 0
    for page in range(MAX_PAGES):
        result = calendar.list_events(start=start, end=end, calendar_ids=calendar_ids,
                                      timezone=timezone, limit=100, position=position)
        if result["not_found"]:
            raise JMAPError("incomplete_availability", "Some occurrences were inaccessible; free time cannot be established.")
        pagination = result["pagination"]
        if query_state is not None and query_state != pagination["query_state"]:
            raise JMAPError("state_changed", "Calendar query changed during scanning; repeat the availability request.")
        query_state = pagination["query_state"]
        if result["state"] is not None:
            states.add(result["state"])
        if len(states) > 1:
            raise JMAPError("state_changed", "Calendar changed during scanning; repeat the availability request.")
        for event in result["data"]:
            if event.get("status") == "cancelled" or event.get("freeBusyStatus") == "free":
                continue
            try:
                a, b = instant(event["utcStart"]), instant(event["utcEnd"])
            except (JMAPError, KeyError):
                raise JMAPError("incomplete_availability", "An occurrence lacks valid server-derived UTC boundaries; free time cannot be established.") from None
            if b < a:
                raise JMAPError("incomplete_availability", "An occurrence has inconsistent UTC boundaries.")
            a, b = max(first, a), min(last, b)
            if a < b:
                periods.append((a, b, event["id"]))
        if not pagination["has_more"]:
            break
        next_position = pagination["next_position"]
        if type(next_position) is not int or next_position <= position:
            raise JMAPError("incomplete_availability", "The server did not advance calendar pagination.")
        position = next_position
    else:
        raise JMAPError("incomplete_availability", "The availability range exceeds the bounded scan limit; request a smaller range.")
    merged = []
    conflicts = []
    active = []
    for a, b, identifier in sorted(periods):
        active = [(other_end, other_id) for other_end, other_id in active if other_end > a]
        for other_end, other_id in active:
            if len(conflicts) == 1000:
                raise JMAPError("incomplete_availability", "Too many conflicts; request a smaller range.")
            conflicts.append({"event_ids": [other_id, identifier], "start": iso(a), "end": iso(min(b, other_end))})
        active.append((b, identifier))
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    data = {"busy": [{"start": iso(a), "end": iso(b)} for a, b in merged], "conflicts": conflicts,
            "timezone": timezone, "scope": "selected_calendars" if calendar_ids else "accessible_account_events",
            "calendar_ids": calendar_ids, "complete": True}
    if include_free:
        free, cursor = [], first
        for a, b in merged:
            if cursor < a:
                free.append({"start": iso(cursor), "end": iso(a)})
            cursor = b
        if cursor < last:
            free.append({"start": iso(cursor), "end": iso(last)})
        data["free"] = free
    return models.envelope(data)
