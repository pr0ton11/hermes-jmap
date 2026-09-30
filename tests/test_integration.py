"""Explicit opt-in Stalwart smoke tests. Read-only; no personal fixtures are saved."""
from datetime import datetime, timedelta, timezone
import importlib
import os
import unittest

from .support import load_plugin

PACKAGE = load_plugin()
Config = importlib.import_module(PACKAGE + ".config").Config
Client = importlib.import_module(PACKAGE + ".client").Client
Mail = importlib.import_module(PACKAGE + ".mail").Mail
Calendar = importlib.import_module(PACKAGE + ".calendar_ops").Calendar
CALENDARS = importlib.import_module(PACKAGE + ".session").CALENDARS


@unittest.skipUnless(os.environ.get("JMAP_INTEGRATION") == "1", "Live Stalwart tests require JMAP_INTEGRATION=1")
class StalwartIntegrationTests(unittest.TestCase):
    def setUp(self):
        class Context:
            def get_config(self, key, default=None):
                names = {"auth_type": "JMAP_AUTH_TYPE", "mail_account_id": "JMAP_MAIL_ACCOUNT_ID", "calendar_account_id": "JMAP_CALENDAR_ACCOUNT_ID"}
                return os.environ.get(names[key], default) if key in names else default
        self.client = Client(Config.from_plugin(Context(), os.environ))

    def test_live_mail_discovery_query_get_and_thread(self):
        mail = Mail(self.client)
        self.assertIsInstance(mail.list_mailboxes()["data"], list)
        page = mail.list_email(limit=1)
        if page["data"]:
            email = page["data"][0]
            self.assertEqual(mail.get_email(email["id"])["data"]["id"], email["id"])
            if email.get("threadId"):
                self.assertIsInstance(mail.get_thread(email["threadId"], limit=1)["data"], list)

    def test_live_calendar_discovery_and_expansion(self):
        if CALENDARS not in self.client.session.capabilities:
            self.skipTest("Server does not advertise calendars")
        calendar = Calendar(self.client)
        self.assertIsInstance(calendar.list_calendars()["data"], list)
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        page = calendar.list_events(start=now.isoformat(), end=(now + timedelta(days=1)).isoformat(), limit=1)
        if page["data"]:
            self.assertEqual(calendar.get_event(page["data"][0]["id"])["data"]["id"], page["data"][0]["id"])
