"""Explicit consequential calendar operations with state guards and scheduling opt-in."""
import re
import uuid

from . import models
from .calendar_ops import Calendar
from .errors import JMAPError
from .session import CALENDARS
from .time_utils import local, zone

DURATION = re.compile(r"^P(?=\d|T\d)(?:\d+W)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$")


def validate_event_times(values, default_timezone):
    timezone = values.get("timeZone", default_timezone)
    if timezone is not None:
        zone(timezone)
    if "start" in values:
        # Floating times are preserved; ambiguity is checked using the user's interpretation.
        local(values["start"], timezone or default_timezone)
    if "duration" in values and not DURATION.fullmatch(values["duration"]):
        raise JMAPError("invalid_duration", "Use a nonnegative JSCalendar duration such as PT1H or P1D.")
    for participant in (values.get("participants") or {}).values():
        if any(key in participant for key in ("email", "roles", "participationStatus", "expectReply")) and not participant.get("calendarAddress"):
            raise JMAPError("invalid_participant", "Scheduling participants require calendarAddress, such as a mailto URI.")


class CalendarMutations:
    def __init__(self, client):
        self.client = client
        self.calendar = Calendar(client)
        self.account_id = client.config.calendar_account_id

    def _rights(self, calendar_id, *, own=True):
        data = self.calendar.get_calendar(calendar_id)["data"]
        rights = data.get("myRights")
        if not isinstance(rights, dict) or not (rights.get("mayWriteAll") is True or own and rights.get("mayWriteOwn") is True):
            raise JMAPError("forbidden", "Calendar write permission is required for this operation.")

    def create_event(self, calendar_id, title, start, duration, timezone="Europe/Zurich", description=None,
                     locations=None, participants=None, recurrence_rule=None, send_scheduling_messages=False):
        self._rights(calendar_id)
        values = {"@type": "Event", "uid": str(uuid.uuid4()), "title": title, "start": start,
                  "duration": duration, "timeZone": timezone, "calendarIds": {calendar_id: True}}
        for key, value in (("description", description), ("locations", locations), ("participants", participants), ("recurrenceRule", recurrence_rule)):
            if value is not None:
                values[key] = value
        validate_event_times(values, self.client.config.timezone)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, create={"event": values}, sendSchedulingMessages=send_scheduling_messages)
        return models.envelope(outcome, action="event_created", scheduling_messages_requested=send_scheduling_messages)

    def _existing(self, event_id, if_in_state):
        result = self.calendar.get_event(event_id)
        if if_in_state is not None and result["state"] != if_in_state:
            raise JMAPError("stateMismatch", "Event state changed; inspect it before retrying.")
        event = result["data"]
        calendars = event.get("calendarIds")
        if not isinstance(calendars, dict) or not calendars:
            raise JMAPError("invalid_event", "Event has no accessible calendar membership.")
        for identifier, included in calendars.items():
            if included:
                self._rights(identifier, own=event.get("isOrigin") is True)
        scope = "occurrence" if event.get("baseEventId") or event.get("recurrenceId") else "series_or_single_event"
        return event, result["state"], scope

    def update_event(self, event_id, changes, if_in_state=None, send_scheduling_messages=False):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify at least one event field to change.")
        event, state, scope = self._existing(event_id, if_in_state)
        mapping = {"timezone": "timeZone", "recurrence_rule": "recurrenceRule", "free_busy_status": "freeBusyStatus"}
        values = {mapping.get(key, key): value for key, value in changes.items()}
        validate_event_times({**event, **values}, self.client.config.timezone)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, if_in_state=state,
                                  update={event_id: values}, sendSchedulingMessages=send_scheduling_messages)
        return models.envelope(outcome, action="event_updated", event_id=event_id, scope=scope, scheduling_messages_requested=send_scheduling_messages)

    def delete_event(self, event_id, if_in_state=None, send_scheduling_messages=False):
        _, state, scope = self._existing(event_id, if_in_state)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, if_in_state=state,
                                  destroy=[event_id], sendSchedulingMessages=send_scheduling_messages)
        return models.envelope(outcome, action="event_deleted", event_id=event_id, scope=scope, scheduling_messages_requested=send_scheduling_messages)
