"""Explicit preview/import of verified email calendar attachments; no local parser."""
import copy

from . import models
from .calendar_mutations import CalendarMutations, validate_event_times
from .errors import JMAPError, malformed
from .mail import Mail
from .session import MAIL, CALENDARS, CALENDAR_PARSE

IMPORT_FIELDS = {"@type", "uid", "title", "description", "descriptionContentType", "start", "duration", "timeZone",
                 "showWithoutTime", "locations", "virtualLocations", "participants", "organizerCalendarAddress",
                 "recurrenceRule", "recurrenceOverrides", "recurrenceId", "status", "freeBusyStatus", "privacy",
                 "sequence", "updated", "created", "prodId", "links", "locale", "keywords", "categories"}


class Invitations(CalendarMutations):
    def _parse(self, email_id, blob_id):
        calendar_account = self.client.session.account(CALENDARS, self.account_id)
        self.client.session.account(CALENDAR_PARSE, calendar_account)
        mail_account = self.client.session.account(MAIL, self.client.config.mail_account_id)
        email = Mail(self.client).get_email(email_id)["data"]
        matches = [part for part in email["attachments"] if part.get("blobId") == blob_id]
        if len(matches) != 1:
            raise JMAPError("not_found", "Select one verified attachment belonging to this email.")
        part = matches[0]
        if part.get("type", "").split(";", 1)[0].casefold() != "text/calendar" and not (part.get("name") or "").casefold().endswith(".ics"):
            raise JMAPError("invalid_attachment", "Select a calendar .ics attachment.")
        size = part.get("size")
        if type(size) is not int or size < 0:
            raise malformed()
        if size > self.client.config.max_attachment_bytes:
            raise JMAPError("attachment_too_large", "The invitation exceeds the configured attachment limit.")
        parsing_blob = blob_id
        if mail_account != calendar_account:
            if not self.client.config.enable_mutations:
                raise JMAPError("mutations_disabled", "Cross-account invitation parsing uploads a temporary blob; enable_mutations must be true.")
            raw = self.client.download(mail_account, blob_id)
            if len(raw) != size:
                raise JMAPError("attachment_size_mismatch", "Invitation attachment bytes differ from its metadata.")
            parsing_blob = self.client.upload(calendar_account, raw, "text/calendar")["blobId"]
        result = self.client.call("CalendarEvent/parse", {"blobIds": [parsing_blob]}, CALENDAR_PARSE,
                                  account_id=calendar_account, extra_capabilities=(CALENDARS,))
        parsed, missing, invalid = result.get("parsed") or {}, result.get("notFound") or [], result.get("notParsable") or []
        if (not isinstance(parsed, dict) or not isinstance(missing, list) or not isinstance(invalid, list)
                or any(not isinstance(value, str) for value in missing + invalid)
                or set(parsed) | set(missing) | set(invalid) != {parsing_blob}
                or (int(parsing_blob in parsed) + int(parsing_blob in missing) + int(parsing_blob in invalid)) != 1):
            raise malformed()
        if parsing_blob in missing:
            raise JMAPError("not_found", "The server could not find the invitation blob.")
        if parsing_blob in invalid:
            raise JMAPError("invalid_invitation", "The server could not parse this calendar attachment.")
        events = parsed[parsing_blob]
        if not isinstance(events, list) or len(events) > 100 or any(not isinstance(event, dict) for event in events):
            raise malformed()
        return events

    def preview_calendar_invitation(self, email_id, blob_id):
        return models.envelope(self._parse(email_id, blob_id), email_id=email_id, blob_id=blob_id,
                               creates_event=False, sends_scheduling_messages=False)

    def import_calendar_invitation(self, email_id, blob_id, calendar_id, uid, recurrence_id=None):
        events = self._parse(email_id, blob_id)
        selected = [event for event in events if event.get("uid") == uid and event.get("recurrenceId") == recurrence_id]
        if len(selected) != 1:
            raise JMAPError("invitation_selection", "Select exactly one UID and recurrence identity from the attachment preview.")
        event = selected[0]
        method = event.get("method")
        if not isinstance(method, str) or method.casefold() not in {"request", "publish"} or event.get("@type", "Event") != "Event":
            raise JMAPError("unsupported_invitation", "Import supports only Event REQUEST/PUBLISH scheduling snapshots.")
        found = self.client.call("CalendarEvent/query", {"filter": {"uid": uid}, "expandRecurrences": False,
                                                       "limit": 100, "calculateTotal": True}, CALENDARS, account_id=self.account_id)
        ids = found.get("ids")
        total = found.get("total")
        if (not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) > 100 or len(ids) != len(set(ids))
                or type(total) is not int or total != len(ids) or not isinstance(found.get("queryState"), str)):
            raise JMAPError("incomplete_scan", "Invitation identity discovery is incomplete; narrow or inspect the account before importing.")
        matches, state = [], None
        batch = self.client.session.limit("maxObjectsInGet", 100)
        for offset in range(0, len(ids), batch):
            got = self.client.get("CalendarEvent", ids[offset:offset + batch], CALENDARS, account_id=self.account_id,
                                  properties=["id", "uid", "recurrenceId"], timeZone=self.client.config.timezone)
            if got["notFound"] or state is not None and state != got["state"]:
                raise JMAPError("state_changed", "Invitation identity discovery changed; repeat the read.")
            state = got["state"]
            matches.extend(item["id"] for item in got["list"] if item.get("uid") == uid and item.get("recurrenceId") == recurrence_id)
        if len(matches) > 1:
            raise JMAPError("invitation_selection", "Several existing events match this invitation identity.")
        if matches:
            return models.envelope({"success": True}, action="invitation_already_present", event_id=matches[0], imported=False,
                                   scheduling_messages_requested=False)
        values = {key: copy.deepcopy(value) for key, value in event.items() if key in IMPORT_FIELDS}
        if "recurrenceOverrides" in values:
            overrides = values["recurrenceOverrides"]
            if not isinstance(overrides, dict) or any(not isinstance(patch, dict) for patch in overrides.values()):
                raise JMAPError("invalid_invitation", "Invitation recurrence overrides must contain event patches.")
            values["recurrenceOverrides"] = {identity: {key: value for key, value in patch.items()
                if key.split("/", 1)[0] in IMPORT_FIELDS | {"excluded"}} for identity, patch in overrides.items()}
        values.update({"@type": "Event", "calendarIds": {calendar_id: True}})
        if any(key not in values for key in ("uid", "start", "duration")):
            raise JMAPError("invalid_invitation", "Invitation is missing its UID, start or duration.")
        validate_event_times(values, self.client.config.timezone)
        self._rights(calendar_id, event=values)
        result = self.client.set("CalendarEvent", CALENDARS, account_id=self.account_id,
                                 create={"invitation": values}, sendSchedulingMessages=False)
        return self._result(result, "invitation_imported", result["created"].get("invitation"), imported=result["success"], scheduling_messages_requested=False)
