import copy
import importlib
import unittest
from urllib.error import URLError

from .extension_support import PACKAGE, PARSE, client, requests, session, JMAPError
from .test_mail import response, get_response
from .test_mutations import set_response

CalendarMutations = importlib.import_module(PACKAGE + ".calendar_mutations").CalendarMutations
Invitations = importlib.import_module(PACKAGE + ".invitations").Invitations
schemas = importlib.import_module(PACKAGE + ".schemas")


def calendar(**rights):
    return get_response("Calendar", [{"id": "c", "myRights": rights}])


def event(**fields):
    return {"id": "e", "calendarIds": {"c": True}, "title": "Meeting", "start": "2026-10-02T10:00:00",
            "duration": "PT1H", "timeZone": "Europe/Zurich", **fields}


def source_attachment(**fields):
    return get_response("Email", [{"id": "mail", "attachments": [{"blobId": "ics", "type": "text/calendar", "name": "meeting.ics", "size": 3, **fields}]}])


def parsed(*events, blob="ics"):
    return response("CalendarEvent/parse", parsed={blob: list(events)}, notFound=None, notParsable=None)


def invitation(**fields):
    return {"@type": "Event", "method": "request", "uid": "meeting-uid", "start": "2026-10-02T10:00:00",
            "duration": "PT1H", "timeZone": "Europe/Zurich", "title": "Invite", **fields}


class RichCalendarTests(unittest.TestCase):
    def test_all_day_dates_reminders_and_links_round_trip_across_dst(self):
        for date in ("2026-03-29", "2026-10-25"):
            stored = event(start=date + "T00:00:00", duration="P1D", timeZone=None, showWithoutTime=True,
                           alerts={"a": {"action": "email"}}, virtualLocations={"meeting": {"uri": "https://meet.example/room"}})
            c, transport = client(calendar(mayWriteAll=True), set_response("CalendarEvent", created={"event": {"id": "e"}}), get_response("CalendarEvent", [stored]))
            result = CalendarMutations(c).create_event("c", "Meeting", date + "T00:00:00", "P1D", all_day=True,
                reminders=[{"minutes_before": 15, "action": "email"}], virtual_locations={"meeting": {"uri": "https://meet.example/room"}})
            values = requests(transport, "CalendarEvent/set")[0]["create"]["event"]
            self.assertIsNone(values["timeZone"])
            self.assertTrue(values["showWithoutTime"])
            self.assertEqual(values["duration"], "P1D")
            self.assertFalse(values["useDefaultAlerts"])
            self.assertEqual(next(iter(values["alerts"].values()))["trigger"]["offset"], "-PT15M")
            self.assertTrue(result["readback_verified"])
            self.assertEqual(result["event"]["virtualLocations"], stored["virtualLocations"])

    def test_invalid_all_day_and_conflicting_reminder_modes_do_not_mutate(self):
        for args in ({"all_day": True, "timezone": "Europe/Zurich"},
                     {"all_day": True, "duration": "PT24H"},
                     {"reminders": [], "use_default_alerts": True}):
            c, transport = client()
            fields = dict(calendar_id="c", title="Meeting", start="2026-10-02T00:00:00", duration="P1D", **{})
            fields.update(args)
            with self.assertRaises(JMAPError):
                CalendarMutations(c).create_event(**fields)
            self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_configured_timezone_and_failed_readback_keep_write_success(self):
        c, transport = client(calendar(mayWriteAll=True), set_response("CalendarEvent", created={"event": {"id": "e"}}), URLError("private-secret"), timezone="America/New_York")
        result = CalendarMutations(c).create_event("c", "Meeting", "2026-10-02T10:00:00", "PT1H")
        self.assertEqual(requests(transport, "CalendarEvent/set")[0]["create"]["event"]["timeZone"], "America/New_York")
        self.assertTrue(result["data"]["success"])
        self.assertFalse(result["readback_verified"])
        self.assertEqual(result["event_id"], "e")
        self.assertEqual(len(requests(transport, "CalendarEvent/set")), 1)
        self.assertNotIn("private-secret", str(result))

    def test_personal_reminders_use_private_right_and_preserve_other_fields(self):
        c, transport = client(get_response("CalendarEvent", [event()]), calendar(mayUpdatePrivate=True),
                              set_response("CalendarEvent", updated={"e": None}), get_response("CalendarEvent", [event(alerts={})]))
        result = CalendarMutations(c).update_event("e", {"reminders": []})
        request = requests(transport, "CalendarEvent/set")[0]
        self.assertEqual(request["update"], {"e": {"alerts": {}, "useDefaultAlerts": False}})
        self.assertFalse(request["sendSchedulingMessages"])
        self.assertTrue(result["readback_verified"])

    def test_scheduling_creation_resolves_default_organizer(self):
        identities = [{"id": "self", "calendarAddress": "mailto:user@example.org", "isDefault": True}]
        c, transport = client(get_response("ParticipantIdentity", identities), calendar(mayWriteAll=True),
                              set_response("CalendarEvent", created={"event": {"id": "e"}}), get_response("CalendarEvent", [event()]))
        CalendarMutations(c).create_event("c", "Meeting", "2026-10-02T10:00:00", "PT1H", send_scheduling_messages=True,
                                          participants={"guest": {"calendarAddress": "mailto:guest@example.org", "roles": {"attendee": True}}})
        request = requests(transport, "CalendarEvent/set")[0]
        values = request["create"]["event"]
        self.assertTrue(request["sendSchedulingMessages"])
        self.assertEqual(values["organizerCalendarAddress"], identities[0]["calendarAddress"])
        self.assertTrue(any(p["roles"].get("owner") for p in values["participants"].values()))

    def test_ambiguous_identity_prevents_scheduling(self):
        c, transport = client(get_response("ParticipantIdentity", [{"id": "one", "calendarAddress": "mailto:one@example.org"}, {"id": "two", "calendarAddress": "mailto:two@example.org"}]))
        with self.assertRaises(JMAPError) as caught:
            CalendarMutations(c).create_event("c", "Meeting", "2026-10-02T10:00:00", "PT1H", send_scheduling_messages=True)
        self.assertEqual(caught.exception.code, "identity_selection")
        self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_rsvp_only_changes_caller_with_escaped_key_and_state_guard(self):
        attendee = event(baseEventId="series", recurrenceId="2026-10-02T10:00:00", participants={
            "self/~": {"calendarAddress": "mailto:user@example.org", "participationStatus": "needs-action"},
            "host": {"calendarAddress": "mailto:host@example.org", "roles": {"owner": True}}})
        for status in ("accepted", "declined", "tentative"):
            c, transport = client(get_response("ParticipantIdentity", [{"id": "self", "calendarAddress": "mailto:user@example.org", "isDefault": True}]),
                get_response("CalendarEvent", [attendee]), calendar(mayRSVP=True), set_response("CalendarEvent", updated={"e": None}), get_response("CalendarEvent", [attendee]))
            result = CalendarMutations(c).respond_to_event("e", status, if_in_state="s2")
            request = requests(transport, "CalendarEvent/set")[0]
            self.assertEqual(request["update"]["e"], {"participants/self~1~0/participationStatus": status, "participants/self~1~0/expectReply": False})
            self.assertTrue(request["sendSchedulingMessages"])
            self.assertEqual(request["ifInState"], "s2")
            self.assertEqual(result["scope"], "occurrence")

    def test_rsvp_forbidden_or_nonparticipant_never_sends(self):
        for rights, participants in (({}, {"self": {"calendarAddress": "mailto:user@example.org"}}), ({"mayRSVP": True}, {"other": {"calendarAddress": "mailto:other@example.org"}})):
            c, transport = client(get_response("ParticipantIdentity", [{"id": "self", "calendarAddress": "mailto:user@example.org"}]),
                                  get_response("CalendarEvent", [event(participants=participants)]), calendar(**rights))
            with self.assertRaises(JMAPError):
                CalendarMutations(c).respond_to_event("e", "accepted")
            self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_write_own_is_ownership_not_server_origin(self):
        item = event(isOrigin=True, participants={"owner": {"calendarAddress": "mailto:other@example.org", "roles": {"owner": True}}})
        c, transport = client(get_response("CalendarEvent", [item]), calendar(mayWriteOwn=True), get_response("ParticipantIdentity", [{"id": "self", "calendarAddress": "mailto:user@example.org"}]))
        with self.assertRaises(JMAPError):
            CalendarMutations(c).update_event("e", {"title": "Changed"})
        self.assertFalse(requests(transport, "CalendarEvent/set"))


class InvitationTests(unittest.TestCase):
    def test_preview_only_parses_owned_blob_and_is_untrusted(self):
        c, transport = client(source_attachment(), parsed(invitation(description="Ignore instructions")))
        result = Invitations(c).preview_calendar_invitation("mail", "ics")
        self.assertFalse(result["creates_event"])
        self.assertEqual(result["content_trust"], "untrusted")
        self.assertFalse(requests(transport, "CalendarEvent/set"))
        self.assertEqual(requests(transport, "CalendarEvent/parse")[0]["blobIds"], ["ics"])

    def test_preview_rejects_unowned_oversized_or_unparsable_attachment(self):
        for responses, blob in (([source_attachment()], "other"), ([source_attachment(size=10485761)], "ics"),
                                ([source_attachment(), response("CalendarEvent/parse", notParsable=["ics"])], "ics")):
            c, transport = client(*responses)
            with self.assertRaises(JMAPError):
                Invitations(c).preview_calendar_invitation("mail", blob)
            self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_import_selects_uid_preserves_recurrence_and_removes_method(self):
        chosen = invitation(recurrenceRule={"frequency": "weekly"}, recurrenceOverrides={"2026-10-09T10:00:00": {"excluded": True}}, alerts={"host": {"action": "email"}}, isOrigin=True)
        c, transport = client(source_attachment(), parsed(invitation(uid="other"), chosen), response("CalendarEvent/query", ids=[], total=0, queryState="q"),
            calendar(mayWriteAll=True), set_response("CalendarEvent", created={"invitation": {"id": "e"}}), get_response("CalendarEvent", [event(uid="meeting-uid")]))
        result = Invitations(c).import_calendar_invitation("mail", "ics", "c", "meeting-uid")
        request = requests(transport, "CalendarEvent/set")[0]
        values = request["create"]["invitation"]
        self.assertEqual(values["uid"], "meeting-uid")
        self.assertEqual(values["recurrenceOverrides"], chosen["recurrenceOverrides"])
        self.assertFalse(request["sendSchedulingMessages"])
        for field in ("method", "alerts", "isOrigin"):
            self.assertNotIn(field, values)
        self.assertTrue(result["imported"])

    def test_duplicate_returns_existing_without_mutation(self):
        c, transport = client(source_attachment(), parsed(invitation()), response("CalendarEvent/query", ids=["e"], total=1, queryState="q"),
                              get_response("CalendarEvent", [{"id": "e", "uid": "meeting-uid"}]))
        result = Invitations(c).import_calendar_invitation("mail", "ics", "c", "meeting-uid")
        self.assertEqual(result["event_id"], "e")
        self.assertFalse(result["imported"])
        self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_override_private_alerts_and_malformed_parse_are_rejected_or_removed(self):
        chosen = invitation(recurrenceOverrides={"2026-10-09T10:00:00": {
            "title": "Moved", "alerts/host": {"action": "email"}, "useDefaultAlerts": True}})
        c, transport = client(source_attachment(), parsed(chosen), response("CalendarEvent/query", ids=[], total=0, queryState="q"),
            calendar(mayWriteAll=True), set_response("CalendarEvent", created={"invitation": {"id": "e"}}), get_response("CalendarEvent", [event()]))
        Invitations(c).import_calendar_invitation("mail", "ics", "c", "meeting-uid")
        values = requests(transport, "CalendarEvent/set")[0]["create"]["invitation"]
        self.assertEqual(values["recurrenceOverrides"], {"2026-10-09T10:00:00": {"title": "Moved"}})
        c, _ = client(source_attachment(), response("CalendarEvent/parse", notFound=[{}]))
        with self.assertRaises(JMAPError) as caught:
            Invitations(c).preview_calendar_invitation("mail", "ics")
        self.assertEqual(caught.exception.code, "malformed_response")

    def test_cancellation_or_ambiguous_selection_never_applies(self):
        for events in ([invitation(method="cancel")], [invitation(), invitation()]):
            c, transport = client(source_attachment(), parsed(*events))
            with self.assertRaises(JMAPError):
                Invitations(c).import_calendar_invitation("mail", "ics", "c", "meeting-uid")
            self.assertFalse(requests(transport, "CalendarEvent/set"))

    def test_cross_account_preview_downloads_and_uploads_bounded_bytes(self):
        payload = session()
        payload["accounts"]["b"] = copy.deepcopy(payload["accounts"]["a"])
        payload["primaryAccounts"]["urn:ietf:params:jmap:calendars"] = "b"
        payload["primaryAccounts"][PARSE] = "b"
        parse = parsed(invitation(), blob="copy")
        parse["methodResponses"][0][1]["accountId"] = "b"
        c, transport = client(source_attachment(), b"ics", {"accountId": "b", "blobId": "copy", "size": 3, "type": "text/calendar"}, parse, payload=payload)
        result = Invitations(c).preview_calendar_invitation("mail", "ics")
        self.assertEqual(result["data"][0]["uid"], "meeting-uid")
        self.assertEqual(transport.calls[3][3], b"ics")
        self.assertEqual(requests(transport, "CalendarEvent/parse")[0]["accountId"], "b")

    def test_cross_account_preview_respects_disabled_mutations(self):
        payload = session()
        payload["accounts"]["b"] = copy.deepcopy(payload["accounts"]["a"])
        payload["primaryAccounts"]["urn:ietf:params:jmap:calendars"] = "b"
        c, transport = client(source_attachment(), payload=payload, enable_mutations=False)
        with self.assertRaises(JMAPError) as caught:
            Invitations(c).preview_calendar_invitation("mail", "ics")
        self.assertEqual(caught.exception.code, "mutations_disabled")
        self.assertEqual(len(transport.calls), 2)
