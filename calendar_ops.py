"""Capability-driven JMAP calendars; all recurrence expansion is server-side."""
from . import models
from .errors import JMAPError, malformed
from .session import CALENDARS
from .time_utils import query_range, zone

EVENT_PROPERTIES = ["id", "baseEventId", "calendarIds", "uid", "title", "description", "start", "duration", "timeZone", "utcStart", "utcEnd", "showWithoutTime", "locations", "participants", "organizerCalendarAddress", "recurrenceRule", "recurrenceOverrides", "recurrenceId", "status", "freeBusyStatus", "isOrigin", "isDraft"]


def event(item):
    return {**{key: item.get(key) for key in EVENT_PROPERTIES}, "organizer": item.get("organizerCalendarAddress"), "end": item.get("utcEnd")}


class Calendar:
    def __init__(self, client):
        self.client = client
        self.account_id = client.config.calendar_account_id

    def _get(self, kind, ids, **kwargs):
        return self.client.get(kind, ids, CALENDARS, account_id=self.account_id, **kwargs)

    def list_calendars(self):
        result = self._get("Calendar", None)
        fields = ("id", "name", "description", "color", "sortOrder", "isSubscribed", "isVisible", "includeInAvailability", "timeZone", "myRights")
        maximum = self.client.session.capabilities["urn:ietf:params:jmap:core"].get("maxObjectsInGet", 100)
        complete = type(maximum) is int and len(result["list"]) < maximum and not result["notFound"]
        return models.envelope([{key: item.get(key) for key in fields} for item in result["list"]], state=result["state"],
                               complete=complete, possibly_truncated=not complete, not_found=result["notFound"])

    def get_calendar(self, calendar_id):
        result = self._get("Calendar", [calendar_id])
        if not result["list"]:
            raise JMAPError("not_found", "Calendar not found or not accessible.")
        fields = ("id", "name", "description", "color", "sortOrder", "isSubscribed", "isVisible", "includeInAvailability", "timeZone", "myRights")
        return models.envelope({key: result["list"][0].get(key) for key in fields}, state=result["state"])

    def get_event(self, event_id, timezone=None):
        timezone = timezone or self.client.config.timezone
        zone(timezone)
        result = self._get("CalendarEvent", [event_id], properties=EVENT_PROPERTIES, timeZone=timezone)
        if not result["list"]:
            raise JMAPError("not_found", "Event not found or not accessible.")
        return models.envelope(event(result["list"][0]), state=result["state"], interpretation_timezone=timezone)

    def list_events(self, **args):
        return self.search_events(**args)

    def search_events(self, start, end, calendar_ids=None, timezone=None, text=None, limit=20, position=0):
        timezone = timezone or self.client.config.timezone
        _, _, (after, before) = query_range(start, end, timezone)
        conditions = [{"after": after}, {"before": before}]
        if calendar_ids is not None:
            if not calendar_ids:
                raise JMAPError("invalid_arguments", "calendar_ids must be nonempty when specified.")
            conditions.append({"operator": "OR", "conditions": [{"inCalendar": cid} for cid in calendar_ids]})
        if text is not None:
            conditions.append({"text": text})
        result = self.client.call("CalendarEvent/query", {
            "filter": {"operator": "AND", "conditions": conditions},
            "sort": [{"property": "start", "isAscending": True}], "expandRecurrences": True,
            "timeZone": timezone, "limit": limit, "position": position, "calculateTotal": True,
        }, CALENDARS, account_id=self.account_id)
        ids, actual, total = result.get("ids"), result.get("position"), result.get("total")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids) or len(ids) > limit:
            raise malformed()
        if type(actual) is not int or actual != position or not isinstance(result.get("queryState"), str):
            raise malformed()
        if total is not None and (type(total) is not int or total < position + len(ids)):
            raise malformed()
        selected = self.client.session.account(CALENDARS, self.account_id)
        data, missing, state = {}, [], None
        batch = self.client.session.limit("maxObjectsInGet", 100)
        for offset in range(0, len(ids), batch):
            got = self._get("CalendarEvent", ids[offset:offset + batch], properties=EVENT_PROPERTIES, timeZone=timezone)
            if state is not None and state != got["state"]:
                raise JMAPError("state_changed", "Calendar state changed during retrieval; repeat the read.")
            state = got["state"]
            data.update({item["id"]: event(item) for item in got["list"]})
            missing.extend(got["notFound"])
        more = position + len(ids) < total if total is not None else len(ids) == limit
        if more and not ids:
            raise malformed()
        return models.envelope([data[key] for key in ids if key in data], not_found=missing,
                               state=state, account_id=selected, interpretation_timezone=timezone,
                               pagination={"position": position, "next_position": position + len(ids) if more else None,
                                           "has_more": more, "total": total, "query_state": result["queryState"]})
