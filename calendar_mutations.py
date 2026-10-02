"""Calendar writes with actor-specific rights, state guards and explicit scheduling."""
import re
import uuid
from urllib.parse import unquote, urlsplit

from . import models
from .calendar_ops import Calendar
from .errors import JMAPError
from .identities import participant_identity, participant_identities
from .session import CALENDARS
from .time_utils import local, zone

DURATION = re.compile(r"^P(?=\d|T\d)(?:\d+W)?(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?$")
UNSET = object()


def calendar_address(value):
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise JMAPError("invalid_participant", "Use a valid calendar scheduling URI.")
    parsed = urlsplit(value)
    if not parsed.scheme:
        raise JMAPError("invalid_participant", "Scheduling participants require a calendar URI such as mailto:person@example.org.")
    if parsed.scheme.lower() == "mailto":
        return "mailto:" + unquote(parsed.path).casefold()
    return parsed._replace(scheme=parsed.scheme.lower(), netloc=parsed.netloc.lower()).geturl()


def validate_event_times(values, default_timezone):
    timezone = values.get("timeZone", default_timezone)
    if timezone is not None:
        zone(timezone)
    if "start" in values:
        local(values["start"], timezone or default_timezone)
    if "duration" in values and (not isinstance(values["duration"], str) or not DURATION.fullmatch(values["duration"])):
        raise JMAPError("invalid_duration", "Use a nonnegative JSCalendar duration such as PT1H or P1D.")
    if values.get("showWithoutTime") is True:
        if (not values.get("start", "").endswith("T00:00:00") or timezone is not None
                or not re.fullmatch(r"P[1-9]\d*D", values.get("duration", ""))):
            raise JMAPError("invalid_all_day", "All-day events require a midnight start, whole-day duration PnD and floating timezone.")
    for participant in (values.get("participants") or {}).values():
        if any(key in participant for key in ("email", "roles", "participationStatus", "expectReply")):
            calendar_address(participant.get("calendarAddress"))


def event_fields(fields):
    mapping = {"timezone": "timeZone", "recurrence_rule": "recurrenceRule", "free_busy_status": "freeBusyStatus",
               "all_day": "showWithoutTime", "virtual_locations": "virtualLocations", "use_default_alerts": "useDefaultAlerts"}
    values = {mapping.get(key, key): value for key, value in fields.items() if key != "reminders"}
    if fields.get("all_day") is True and "timezone" not in fields:
        values["timeZone"] = None
    if "reminders" in fields:
        if fields.get("use_default_alerts") is True:
            raise JMAPError("invalid_arguments", "Choose explicit reminders or default alerts, not both.")
        values["alerts"] = {str(uuid.uuid4()): {"@type": "Alert", "action": reminder["action"],
                            "trigger": {"@type": "OffsetTrigger", "offset": "-PT" + str(reminder["minutes_before"]) + "M", "relativeTo": "start"}}
                            for reminder in fields["reminders"]}
        values["useDefaultAlerts"] = False
    return values


class CalendarMutations:
    def __init__(self, client):
        self.client = client
        self.calendar = Calendar(client)
        self.account_id = client.config.calendar_account_id

    def _rights(self, calendar_id, *, event=None, permission="write"):
        rights = self.calendar.get_calendar(calendar_id)["data"].get("myRights")
        if not isinstance(rights, dict):
            raise JMAPError("forbidden", "Calendar permission is required for this operation.")
        if rights.get("mayWriteAll") is True or permission != "write" and rights.get(permission) is True:
            return
        if permission == "write" and rights.get("mayWriteOwn") is True:
            owners = [p for p in ((event or {}).get("participants") or {}).values() if (p.get("roles") or {}).get("owner") is True]
            if not owners:
                return
            identities = participant_identities(self.client)
            if identities["complete"]:
                addresses = {calendar_address(i["calendarAddress"]) for i in identities["data"]}
                if any(calendar_address(p.get("calendarAddress")) in addresses for p in owners):
                    return
        raise JMAPError("forbidden", "Calendar permission is required for this operation.")

    def _result(self, outcome, action, event_id=None, **metadata):
        result = models.envelope(outcome, action=action if outcome["success"] else action + "_failed", **metadata)
        if event_id is not None:
            result["event_id"] = event_id
        if outcome["success"] and event_id:
            try:
                got = self.calendar.get_event(event_id)
                result.update(event=got["data"], readback_state=got["state"], readback_verified=True)
            except JMAPError as error:
                result.update(readback_verified=False, readback_error=error.as_dict())
            except Exception:
                result.update(readback_verified=False, readback_error={"code": "readback_failed", "error": "The write succeeded but readback failed. Inspect the returned event ID; do not recreate it."})
        return result

    def create_event(self, calendar_id, title, start, duration, timezone=UNSET, description=None,
                     locations=None, participants=None, recurrence_rule=None, send_scheduling_messages=False,
                     all_day=False, reminders=None, use_default_alerts=None, virtual_locations=None,
                     participant_identity_id=None):
        fields = {"title": title, "start": start, "duration": duration, "all_day": all_day,
                  "timezone": (None if all_day else self.client.config.timezone) if timezone is UNSET else timezone}
        for key, value in (("description", description), ("locations", locations), ("participants", participants),
                           ("recurrence_rule", recurrence_rule), ("reminders", reminders),
                           ("use_default_alerts", use_default_alerts), ("virtual_locations", virtual_locations)):
            if value is not None:
                fields[key] = value
        values = {"@type": "Event", "uid": str(uuid.uuid4()), "calendarIds": {calendar_id: True}, **event_fields(fields)}
        if send_scheduling_messages or participant_identity_id:
            identity = participant_identity(self.client, participant_identity_id)
            address = identity["calendarAddress"]
            values["organizerCalendarAddress"] = address
            values["participants"] = {key: dict(value) for key, value in (participants or {}).items()}
            owners = [p for p in values["participants"].values() if (p.get("roles") or {}).get("owner") is True]
            if any(calendar_address(p.get("calendarAddress")) != calendar_address(address) for p in owners):
                raise JMAPError("identity_mismatch", "Event ownership must match the selected organizer identity.")
            matching = [key for key, p in values["participants"].items() if calendar_address(p.get("calendarAddress")) == calendar_address(address)]
            key = matching[0] if matching else str(uuid.uuid4())
            owner = values["participants"].get(key, {})
            values["participants"][key] = {**owner, "calendarAddress": address, "roles": {**(owner.get("roles") or {}), "owner": True}, "participationStatus": "accepted"}
        validate_event_times(values, self.client.config.timezone)
        self._rights(calendar_id, event=values)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, create={"event": values}, sendSchedulingMessages=send_scheduling_messages)
        return self._result(outcome, "event_created", outcome["created"].get("event"), scheduling_messages_requested=send_scheduling_messages)

    def _existing(self, event_id, if_in_state, permission="write"):
        result = self.calendar.get_event(event_id)
        if if_in_state is not None and result["state"] != if_in_state:
            raise JMAPError("stateMismatch", "Event state changed; inspect it before retrying.")
        event = result["data"]
        calendars = event.get("calendarIds")
        if not isinstance(calendars, dict) or not any(value is True for value in calendars.values()):
            raise JMAPError("invalid_event", "Event has no accessible calendar membership.")
        for identifier, included in calendars.items():
            if included is True:
                self._rights(identifier, event=event, permission=permission)
        scope = "occurrence" if event.get("baseEventId") or event.get("recurrenceId") else "series_or_single_event"
        return event, result["state"], scope

    def update_event(self, event_id, changes, if_in_state=None, send_scheduling_messages=False):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify at least one event field to change.")
        private = set(changes) <= {"reminders", "use_default_alerts"}
        if private and send_scheduling_messages:
            raise JMAPError("invalid_arguments", "Personal reminder updates do not send scheduling messages.")
        event, state, scope = self._existing(event_id, if_in_state, "mayUpdatePrivate" if private else "write")
        values = event_fields(changes)
        if not private:
            validate_event_times({**event, **values}, self.client.config.timezone)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, if_in_state=state,
                                  update={event_id: values}, sendSchedulingMessages=send_scheduling_messages)
        return self._result(outcome, "event_updated", event_id, scope=scope, scheduling_messages_requested=send_scheduling_messages)

    def delete_event(self, event_id, if_in_state=None, send_scheduling_messages=False):
        _, state, scope = self._existing(event_id, if_in_state)
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, if_in_state=state,
                                  destroy=[event_id], sendSchedulingMessages=send_scheduling_messages)
        return models.envelope(outcome, action="event_deleted" if outcome["success"] else "event_deletion_failed", event_id=event_id,
                               scope=scope, scheduling_messages_requested=send_scheduling_messages)

    def respond_to_event(self, event_id, participation_status, participant_identity_id=None, if_in_state=None):
        identity = participant_identity(self.client, participant_identity_id)
        event, state, scope = self._existing(event_id, if_in_state, "mayRSVP")
        matches = [key for key, participant in (event.get("participants") or {}).items()
                   if calendar_address(participant.get("calendarAddress")) == calendar_address(identity["calendarAddress"])]
        if len(matches) != 1:
            raise JMAPError("participant_selection", "The selected identity must match exactly one existing participant.")
        key = matches[0].replace("~", "~0").replace("/", "~1")
        outcome = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id, if_in_state=state,
                                  update={event_id: {"participants/" + key + "/participationStatus": participation_status,
                                                     "participants/" + key + "/expectReply": False}}, sendSchedulingMessages=True)
        return self._result(outcome, "event_response_sent", event_id, scope=scope, scheduling_messages_requested=True)
