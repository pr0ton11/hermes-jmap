import importlib
import json
import unittest
from types import SimpleNamespace

from .support import load_plugin, session_payload
from .test_foundation import Transport
from .test_mail import get_response, response

PACKAGE = load_plugin()
Config = importlib.import_module(PACKAGE + ".config").Config
Client = importlib.import_module(PACKAGE + ".client").Client
Calendar = importlib.import_module(PACKAGE + ".calendar_ops").Calendar
availability = importlib.import_module(PACKAGE + ".availability").calendar_availability
time_utils = importlib.import_module(PACKAGE + ".time_utils")
JMAPError = importlib.import_module(PACKAGE + ".errors").JMAPError
CAP = "urn:ietf:params:jmap:calendars"


def calendar_session():
    data = session_payload()
    data["capabilities"][CAP] = {}
    data["accounts"]["a"]["accountCapabilities"][CAP] = {}
    data["primaryAccounts"][CAP] = "a"
    return data


def occurrence(identifier="e", utc_start="2026-10-01T08:00:00Z", utc_end="2026-10-01T09:00:00Z", **kwargs):
    return {"id": identifier, "utcStart": utc_start, "utcEnd": utc_end, "freeBusyStatus": "busy", **kwargs}


class CalendarTests(unittest.TestCase):
    def calendar(self, *responses):
        transport = Transport(calendar_session(), *responses)
        calendar = Calendar(Client(Config("https://mail.example/session", "user", "secret"), transport))
        return calendar, transport

    def test_calendar_discovery_and_get(self):
        calendar, transport = self.calendar(get_response("Calendar", [{"id": "c", "name": "Personal", "timeZone": "Europe/Zurich"}]))
        self.assertEqual(calendar.list_calendars()["data"][0]["timeZone"], "Europe/Zurich")
        self.assertEqual(json.loads(transport.calls[1][3])["methodCalls"][0][0], "Calendar/get")
        calendar, _ = self.calendar(get_response("Calendar", [], ["c"]))
        with self.assertRaises(JMAPError):
            calendar.get_calendar("c")

    def test_absent_calendar_capability_does_not_fallback(self):
        transport = Transport(session_payload())
        with self.assertRaises(JMAPError) as caught:
            Calendar(Client(Config("https://mail.example/session", "user", "secret"), transport)).list_calendars()
        self.assertEqual(caught.exception.code, "unsupported_capability")
        self.assertEqual(len(transport.calls), 1)

    def test_query_expands_recurrences_and_uses_local_boundaries(self):
        item = occurrence("instance", baseEventId="series", recurrenceId="2026-10-01T10:00:00", start="2026-10-01T10:00:00", timeZone="Europe/Zurich", duration="PT1H")
        calendar, transport = self.calendar(response("CalendarEvent/query", ids=["instance"], position=0, total=1, queryState="q"), get_response("CalendarEvent", [item]))
        result = calendar.search_events(start="2026-10-01T00:00:00Z", end="2026-10-02T00:00:00Z", calendar_ids=["c"], text="dentist")
        self.assertEqual(result["data"][0]["recurrenceId"], "2026-10-01T10:00:00")
        query = json.loads(transport.calls[1][3])["methodCalls"][0][1]
        self.assertTrue(query["expandRecurrences"])
        self.assertEqual(query["timeZone"], "Europe/Zurich")
        self.assertEqual(query["filter"]["conditions"][0], {"after": "2026-10-01T02:00:00"})
        self.assertIn("utcEnd", json.loads(transport.calls[2][3])["methodCalls"][0][1]["properties"])

    def test_get_preserves_floating_and_recurrence_metadata(self):
        item = occurrence(start="2026-10-01T10:00:00", timeZone=None, duration="PT1H", recurrenceRule={"frequency": "daily"}, recurrenceOverrides={"2026-10-02T10:00:00": {"excluded": True}})
        calendar, _ = self.calendar(get_response("CalendarEvent", [item]))
        result = calendar.get_event("e")["data"]
        self.assertIsNone(result["timeZone"])
        self.assertEqual(result["recurrenceRule"]["frequency"], "daily")
        self.assertTrue(result["recurrenceOverrides"]["2026-10-02T10:00:00"]["excluded"])

    def test_dst_and_timezone_input(self):
        for wall in ("2026-03-29T02:30:00", "2026-10-25T02:30:00"):
            with self.subTest(wall=wall), self.assertRaises(JMAPError):
                time_utils.local(wall, "Europe/Zurich")
        self.assertEqual(time_utils.iso(time_utils.local("2026-03-29T03:30:00", "Europe/Zurich")), "2026-03-29T01:30:00Z")
        with self.assertRaises(JMAPError):
            time_utils.zone("Invalid/Timezone")
        with self.assertRaises(JMAPError):
            time_utils.query_range("2026-10-01T10:00:00Z", "2026-10-01T09:00:00Z", "Europe/Zurich")

    def fake_calendar(self, pages):
        iterator = iter(pages)
        return SimpleNamespace(client=SimpleNamespace(config=SimpleNamespace(timezone="Europe/Zurich")), list_events=lambda **kwargs: next(iterator))

    def page(self, events, more=False, position=0, state="s", query="q", missing=None):
        return {"data": events, "state": state, "not_found": missing or [], "pagination": {"query_state": query, "has_more": more, "next_position": position + len(events) if more else None}}

    def test_availability_merge_clip_free_conflicts_and_cancelled(self):
        events = [occurrence("a"), occurrence("b", "2026-10-01T08:30:00Z", "2026-10-01T10:00:00Z"),
                  occurrence("cancelled", status="cancelled"), occurrence("free", freeBusyStatus="free")]
        result = availability(self.fake_calendar([self.page(events)]), "2026-10-01T08:00:00Z", "2026-10-01T11:00:00Z")["data"]
        self.assertEqual(result["busy"], [{"start": "2026-10-01T08:00:00Z", "end": "2026-10-01T10:00:00Z"}])
        self.assertEqual(result["free"], [{"start": "2026-10-01T10:00:00Z", "end": "2026-10-01T11:00:00Z"}])
        self.assertEqual(result["conflicts"][0]["event_ids"], ["a", "b"])

    def test_availability_refuses_missing_changed_or_unknown_times(self):
        pagesets = [[self.page([], missing=["gone"])],
                    [self.page([occurrence()], more=True), self.page([], query="changed")],
                    [self.page([occurrence()], more=True), self.page([], state="changed")],
                    [self.page([{"id": "e", "start": "2026-10-01T10:00:00", "timeZone": None}])]]
        for pages in pagesets:
            with self.subTest(pages=pages), self.assertRaises(JMAPError):
                availability(self.fake_calendar(pages), "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")

    def test_all_day_dst_uses_server_boundaries(self):
        event = occurrence("all-day", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", start="2026-03-29T00:00:00", duration="P1D", timeZone=None, showWithoutTime=True)
        result = availability(self.fake_calendar([self.page([event])]), "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z")["data"]
        self.assertEqual(result["free"], [])
        self.assertEqual(result["busy"][0]["end"], "2026-03-29T22:00:00Z")

    def test_empty_calendar_is_free_and_selected_scope(self):
        result = availability(self.fake_calendar([self.page([])]), "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", calendar_ids=["c"])["data"]
        self.assertEqual(result["busy"], [])
        self.assertEqual(len(result["free"]), 1)
        self.assertEqual(result["scope"], "selected_calendars")

    def test_availability_uses_moved_occurrence_only_not_original_rule_time(self):
        moved = occurrence("moved", "2026-10-01T13:00:00Z", "2026-10-01T14:00:00Z", baseEventId="series", recurrenceId="2026-10-01T10:00:00", start="2026-10-01T15:00:00", timeZone="Europe/Zurich", duration="PT1H")
        calendar, _ = self.calendar(response("CalendarEvent/query", ids=["moved"], position=0, total=1, queryState="q"), get_response("CalendarEvent", [moved]))
        result = availability(calendar, "2026-10-01T08:00:00Z", "2026-10-01T16:00:00Z")["data"]
        self.assertEqual(result["busy"], [{"start": "2026-10-01T13:00:00Z", "end": "2026-10-01T14:00:00Z"}])
        self.assertEqual(result["free"][0]["end"], "2026-10-01T13:00:00Z")
