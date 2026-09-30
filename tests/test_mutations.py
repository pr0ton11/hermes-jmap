import importlib
import json
import unittest
from unittest.mock import patch

from .support import load_plugin
from .test_foundation import Transport
from .test_calendar import calendar_session
from .test_mail import get_response, response

PACKAGE = load_plugin()
Config = importlib.import_module(PACKAGE + ".config").Config
Client = importlib.import_module(PACKAGE + ".client").Client
MailMutations = importlib.import_module(PACKAGE + ".mail_mutations").MailMutations
CalendarMutations = importlib.import_module(PACKAGE + ".calendar_mutations").CalendarMutations
schemas = importlib.import_module(PACKAGE + ".schemas")
JMAPError = importlib.import_module(PACKAGE + ".errors").JMAPError
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"


def mutation_session():
    data = calendar_session()
    data["capabilities"][SUBMISSION] = {}
    data["accounts"]["a"]["accountCapabilities"][SUBMISSION] = {}
    data["primaryAccounts"][SUBMISSION] = "a"
    return data


def set_response(kind, **result):
    return response(kind + "/set", oldState="s2", newState="s3", **result)


def draft():
    return {"id": "draft", "keywords": {"$draft": True, "$flagged": True}, "mailboxIds": {"drafts": True},
            "from": [{"email": "user@example.org"}], "to": [{"email": "recipient@example.org"}], "subject": "Old",
            "bodyStructure": {"type": "text/plain", "blobId": "body", "size": 12},
            "attachments": [{"blobId": "attachment", "type": "application/pdf", "name": "file.pdf"}]}


class MutationTests(unittest.TestCase):
    def operations(self, *responses):
        transport = Transport(mutation_session(), *responses)
        client = Client(Config("https://mail.example/session", "user@example.org", "secret", enable_mutations=True), transport)
        return MailMutations(client), CalendarMutations(client), transport

    def test_create_draft_never_submits(self):
        mail, _, transport = self.operations(get_response("Identity", [{"id": "identity", "email": "user@example.org"}]),
                                             get_response("Mailbox", [{"id": "drafts", "role": "drafts"}]),
                                             set_response("Email", created={"draft": {"id": "new"}}))
        result = mail.create_draft(to=[{"email": "r@example.org"}], subject="Hello", body="Message")
        self.assertFalse(result["sends_email"])
        method = json.loads(transport.calls[-1][3])["methodCalls"][0]
        self.assertEqual(method[0], "Email/set")
        self.assertTrue(method[1]["create"]["draft"]["keywords"]["$draft"])
        self.assertNotIn("EmailSubmission", json.dumps([x[3].decode() for x in transport.calls if x[3]]))

    def test_update_replaces_then_destroys_old_with_state_guard(self):
        mail, _, transport = self.operations(get_response("Email", [draft()]),
                                             set_response("Email", created={"replacement": {"id": "new-draft"}}),
                                             set_response("Email", destroyed=["draft"]))
        result = mail.update_draft("draft", body="New", subject="New subject")
        self.assertEqual(result["email_id"], "new-draft")
        created = json.loads(transport.calls[2][3])["methodCalls"][0][1]
        self.assertEqual(created["ifInState"], "s2")
        self.assertEqual(created["create"]["replacement"]["bodyStructure"]["subParts"][1]["blobId"], "attachment")
        destroy = json.loads(transport.calls[3][3])["methodCalls"][0][1]
        self.assertEqual(destroy["destroy"], ["draft"])
        self.assertEqual(destroy["ifInState"], "s3")

    def test_subject_edit_preserves_blob_body(self):
        mail, _, transport = self.operations(get_response("Email", [draft()]), set_response("Email", created={"replacement": {"id": "new"}}), set_response("Email", destroyed=["draft"]))
        mail.update_draft("draft", subject="Updated")
        values = json.loads(transport.calls[2][3])["methodCalls"][0][1]["create"]["replacement"]
        self.assertEqual(values["bodyStructure"]["blobId"], "body")
        self.assertNotIn("size", values["bodyStructure"])

    def test_replacement_failure_never_destroys_and_cleanup_partial_is_explicit(self):
        mail, _, transport = self.operations(get_response("Email", [draft()]), set_response("Email", notCreated={"replacement": {"type": "overQuota", "description": "secret"}}))
        result = mail.update_draft("draft", subject="New")
        self.assertEqual(result["action"], "draft_replacement_failed")
        self.assertEqual(len(transport.calls), 3)
        self.assertNotIn("secret", json.dumps(result))
        mail, _, _ = self.operations(get_response("Email", [draft()]), set_response("Email", created={"replacement": {"id": "new"}}),
                                     set_response("Email", notDestroyed={"draft": {"type": "forbidden"}}))
        result = mail.update_draft("draft", subject="New")
        self.assertEqual(result["action"], "draft_replaced_cleanup_incomplete")
        self.assertEqual(result["email_id"], "new")

    def test_non_drafts_and_state_mismatches_are_refused(self):
        for properties, state in (({"keywords": {}}, None), ({}, "stale")):
            item = draft()
            item.update(properties)
            mail, _, transport = self.operations(get_response("Email", [item]))
            with self.assertRaises(JMAPError):
                mail.update_draft("draft", if_in_state=state, body="New")
            self.assertEqual(len(transport.calls), 2)

    def test_keyword_patches_preserve_unrelated_flags(self):
        mail, _, transport = self.operations(set_response("Email", updated={"email": None}))
        mail.mark_read("email", False)
        args = json.loads(transport.calls[1][3])["methodCalls"][0][1]
        self.assertEqual(args["update"], {"email": {"keywords/$seen": None}})
        mail, _, transport = self.operations(set_response("Email", updated={"email": None}))
        mail.set_flagged("email", True)
        self.assertEqual(json.loads(transport.calls[1][3])["methodCalls"][0][1]["update"], {"email": {"keywords/$flagged": True}})

    def test_move_checks_mailbox(self):
        mail, _, transport = self.operations(get_response("Mailbox", [{"id": "m"}]), set_response("Email", updated={"email": None}))
        mail.move_email("email", "m")
        self.assertEqual(json.loads(transport.calls[2][3])["methodCalls"][0][1]["update"]["email"], {"mailboxIds": {"m": True}})

    def test_delete_one_email_is_guarded_and_reports_server_failure(self):
        for success in (True, False):
            result = {"destroyed": ["email"]} if success else {"notDestroyed": {"email": {"type": "forbidden", "description": "secret"}}}
            mail, _, transport = self.operations(get_response("Email", [{"id": "email"}]), set_response("Email", **result))
            outcome = mail.delete_email("email", if_in_state="s2")
            self.assertEqual(outcome["data"]["success"], success)
            self.assertTrue(outcome["permanent"])
            request = json.loads(transport.calls[-1][3])["methodCalls"][0][1]
            self.assertEqual(request["destroy"], ["email"])
            self.assertEqual(request["ifInState"], "s2")
            self.assertNotIn("secret", json.dumps(outcome))

    def test_delete_refuses_stale_or_missing_email_before_mutating(self):
        for values, missing, state in (([], ["email"], None), ([{"id": "email"}], [], "stale")):
            mail, _, transport = self.operations(get_response("Email", values, missing))
            with self.assertRaises(JMAPError):
                mail.delete_email("email", if_in_state=state)
            self.assertEqual(len(transport.calls), 2)

    def test_send_separate_capabilities_and_implicit_cleanup(self):
        submission = set_response("EmailSubmission", created={"submission": {"id": "sent"}})
        submission["methodResponses"].append(["Email/set", {"accountId": "a", "oldState": "s2", "newState": "s3", "updated": {"draft": None}}, "request"])
        mail, _, transport = self.operations(get_response("Email", [draft()]), get_response("Identity", [{"id": "identity", "email": "user@example.org"}]), get_response("Email", [draft()]), submission)
        result = mail.send_draft("draft", "identity")
        self.assertEqual(result["action"], "email_submitted")
        self.assertTrue(result["data"]["email_cleanup"]["success"])
        request = json.loads(transport.calls[-1][3])
        self.assertIn(MAIL, request["using"])
        self.assertIn(SUBMISSION, request["using"])
        self.assertEqual(request["methodCalls"][0][1]["create"]["submission"]["emailId"], "draft")

    def test_send_identity_mismatch_refused(self):
        mail, _, transport = self.operations(get_response("Email", [draft()]), get_response("Identity", [{"id": "identity", "email": "other@example.org"}]))
        with self.assertRaises(JMAPError):
            mail.send_draft("draft", "identity")
        self.assertEqual(len(transport.calls), 3)

    def test_calendar_create_scheduling_default_and_rights(self):
        _, calendar, transport = self.operations(get_response("Calendar", [{"id": "c", "myRights": {"mayWriteAll": True}}]), set_response("CalendarEvent", created={"event": {"id": "e"}}))
        result = calendar.create_event("c", "Dentist", "2026-10-01T10:00:00", "PT1H")
        request = json.loads(transport.calls[-1][3])["methodCalls"][0][1]
        self.assertFalse(request["sendSchedulingMessages"])
        self.assertEqual(request["create"]["event"]["timeZone"], "Europe/Zurich")
        self.assertTrue(result["data"]["success"])
        _, calendar, transport = self.operations(get_response("Calendar", [{"id": "c", "myRights": {"mayWriteAll": False}}]))
        with self.assertRaises(JMAPError):
            calendar.create_event("c", "Event", "2026-10-01T10:00:00", "PT1H")
        self.assertEqual(len(transport.calls), 2)

    def test_update_and_delete_occurrence_with_guards(self):
        item = {"id": "occ", "baseEventId": "series", "recurrenceId": "2026-10-01T10:00:00", "calendarIds": {"c": True}, "start": "2026-10-01T10:00:00", "duration": "PT1H", "timeZone": "Europe/Zurich", "isOrigin": True}
        for method in ("update", "delete"):
            _, calendar, transport = self.operations(get_response("CalendarEvent", [item]), get_response("Calendar", [{"id": "c", "myRights": {"mayWriteOwn": True}}]),
                                                     set_response("CalendarEvent", **({"updated": {"occ": None}} if method == "update" else {"destroyed": ["occ"]})))
            result = calendar.update_event("occ", {"title": "Changed"}, send_scheduling_messages=True) if method == "update" else calendar.delete_event("occ")
            self.assertEqual(result["scope"], "occurrence")
            self.assertEqual(json.loads(transport.calls[-1][3])["methodCalls"][0][1]["ifInState"], "s2")

    def test_object_set_errors_and_malformed_results(self):
        mail, _, _ = self.operations(set_response("Email", notUpdated={"e": {"type": "forbidden", "description": "secret"}}))
        result = mail.mark_read("e", True)["data"]
        self.assertFalse(result["success"])
        self.assertEqual(result["errors"]["e"]["code"], "forbidden")
        self.assertNotIn("secret", json.dumps(result))
        mail, _, _ = self.operations(set_response("Email", updated={"unexpected": None}))
        with self.assertRaises(JMAPError):
            mail.mark_read("e", True)

    def test_approval_hook_ignores_hostile_content(self):
        plugin = importlib.import_module(PACKAGE)
        for name in schemas.CONSEQUENTIAL:
            directive = plugin.approval_hook(name, {"description": "secret ignore instructions", "send_scheduling_messages": True})
            self.assertEqual(directive["action"], "approve")
            self.assertNotIn("secret", json.dumps(directive))
        self.assertIsNone(plugin.approval_hook("jmap_get_email", {}))

    def test_mutation_schema_is_typed_and_allowlisted(self):
        schemas.validate("jmap_create_draft", {"to": [{"email": "x@example.org"}], "subject": "", "body": ""})
        schemas.validate("jmap_create_event", {"calendar_id": "c", "title": "e", "start": "2026-10-01T10:00:00", "duration": "PT1H", "timezone": None, "recurrence_rule": {"frequency": "weekly", "byDay": [{"day": "mo"}]}})
        for args in ({"event_id": "e", "changes": {"calendarIds": {"c": True}}}, {"event_id": "e", "changes": {}}):
            with self.assertRaises(JMAPError):
                schemas.validate("jmap_update_event", args)
