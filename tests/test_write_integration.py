"""Opt-in tests create temporary objects only in explicitly selected test targets.

These tests call trusted Python handlers and bypass Hermes approval hooks.
They never send mail or scheduling messages. Known created IDs are cleaned up.
"""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid

from .test_integration import PACKAGE, Config, Client


def live_client():
    class Context:
        def get_config(self, key, default=None):
            if key == "enable_mutations":
                return True
            names = {"auth_type": "JMAP_AUTH_TYPE", "mail_account_id": "JMAP_MAIL_ACCOUNT_ID",
                     "calendar_account_id": "JMAP_CALENDAR_ACCOUNT_ID", "contacts_account_id": "JMAP_CONTACTS_ACCOUNT_ID"}
            return os.environ.get(names[key], default) if key in names else default
    return Client(Config.from_plugin(Context(), os.environ))


@unittest.skipUnless(os.environ.get("JMAP_WRITE_INTEGRATION") == "1", "Writes require JMAP_WRITE_INTEGRATION=1")
class StalwartWriteIntegrationTests(unittest.TestCase):
    def test_attachment_and_unsent_draft_round_trip(self):
        identity = os.environ.get("JMAP_TEST_IDENTITY_ID")
        if not identity:
            self.skipTest("Select an isolated JMAP_TEST_IDENTITY_ID")
        operations = importlib.import_module(PACKAGE + ".mail_workflows").MailWorkflows(live_client())
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.txt"
            path.write_bytes(b"hermes-jmap attachment test")
            uploaded = operations.upload_attachment(str(path), content_type="text/plain")["data"]
        result = operations.create_draft([{"email": "test@example.invalid"}], "hermes-jmap test " + str(uuid.uuid4()),
                                         "Unsent integration test", identity_id=identity, attachments=[uploaded])
        identifier = result.get("email_id")
        self.assertIsInstance(identifier, str)
        try:
            self.assertTrue(result["data"]["success"])
            mail = importlib.import_module(PACKAGE + ".mail").Mail(operations.client)
            draft = mail.get_email(identifier)["data"]
            self.assertTrue(draft["keywords"].get("$draft"))
            self.assertTrue(any(part.get("blobId") == uploaded["blob_id"] for part in draft["attachments"]))
        finally:
            self.assertTrue(operations.delete_email(identifier)["data"]["success"])

    def test_calendar_round_trip(self):
        target = os.environ.get("JMAP_TEST_CALENDAR_ID")
        if not target:
            self.skipTest("Select an isolated JMAP_TEST_CALENDAR_ID")
        operations = importlib.import_module(PACKAGE + ".calendar_mutations").CalendarMutations(live_client())
        result = operations.create_event(target, "hermes-jmap test " + str(uuid.uuid4()), "2030-01-10T10:00:00", "PT1H",
                                        reminders=[{"minutes_before": 15, "action": "display"}])
        identifier = result.get("event_id")
        self.assertIsInstance(identifier, str)
        try:
            self.assertTrue(result["data"]["success"])
            self.assertTrue(result["readback_verified"])
            updated = operations.update_event(identifier, {"reminders": [], "title": "hermes-jmap test updated"})
            self.assertTrue(updated["data"]["success"])
            self.assertTrue(updated["readback_verified"])
            self.assertEqual(updated["event"]["title"], "hermes-jmap test updated")
        finally:
            self.assertTrue(operations.delete_event(identifier)["data"]["success"])

    def test_contact_round_trip(self):
        target = os.environ.get("JMAP_TEST_ADDRESS_BOOK_ID")
        if not target:
            self.skipTest("Select an isolated JMAP_TEST_ADDRESS_BOOK_ID")
        operations = importlib.import_module(PACKAGE + ".contacts").Contacts(live_client())
        result = operations.create_contact(target, "hermes-jmap test " + str(uuid.uuid4()),
                                           emails=[{"address": "test@example.invalid"}])
        identifier = result["data"]["created"].get("contact")
        self.assertIsInstance(identifier, str)
        try:
            self.assertTrue(result["data"]["success"])
            self.assertTrue(operations.update_contact(identifier, {"full_name": "hermes-jmap test updated"})["data"]["success"])
            self.assertEqual(operations.get_contact(identifier)["data"]["name"]["full"], "hermes-jmap test updated")
        finally:
            self.assertTrue(operations.delete_contact(identifier)["data"]["success"])


@unittest.skipUnless(os.environ.get("JMAP_SCHEDULING_INTEGRATION") == "1", "RSVP requires separate scheduling opt-in")
class StalwartSchedulingIntegrationTests(unittest.TestCase):
    def test_explicit_test_invitation_rsvp(self):
        identifier = os.environ.get("JMAP_TEST_RSVP_EVENT_ID")
        identity = os.environ.get("JMAP_TEST_PARTICIPANT_IDENTITY_ID")
        if not identifier or not identity:
            self.skipTest("Select a test invitation and participant identity explicitly")
        operations = importlib.import_module(PACKAGE + ".calendar_mutations").CalendarMutations(live_client())
        result = operations.respond_to_event(identifier, "tentative", participant_identity_id=identity)
        self.assertTrue(result["data"]["success"])
        self.assertTrue(result["readback_verified"])
