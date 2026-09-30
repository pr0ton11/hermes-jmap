import importlib
import io
import json
import logging
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from .support import load_plugin, session_payload
from .test_foundation import Transport
from .test_mail import get_response

PACKAGE = load_plugin()
client_module = importlib.import_module(PACKAGE + ".client")
handlers = importlib.import_module(PACKAGE + ".handlers")
Config = importlib.import_module(PACKAGE + ".config").Config
Client = client_module.Client
JMAPError = importlib.import_module(PACKAGE + ".errors").JMAPError
MAIL = "urn:ietf:params:jmap:mail"


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}


class Context:
    def __init__(self, mutations=False):
        self.mutations = mutations

    def get_config(self, key, default=None):
        return self.mutations if key == "enable_mutations" else default


class HardeningTests(unittest.TestCase):
    def test_transport_enforces_declared_and_streamed_size(self):
        for headers in ({"Content-Length": "11"}, {}, {"Content-Length": "bogus"}):
            response = FakeResponse(b"x" * 11, headers)
            with self.subTest(headers=headers), patch.object(client_module, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaises(JMAPError):
                    client_module.HTTPTransport().request("GET", "https://mail.example", {}, None, 10, 10)
        response = FakeResponse(b"123", {"Content-Length": "3"})
        with patch.object(client_module, "build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(client_module.HTTPTransport().request("GET", "https://mail.example", {}, None, 10, 10), b"123")

    def test_redirects_do_not_forward_authentication(self):
        self.assertIsNone(client_module.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://attacker.example"))
        transport = Transport(HTTPError("https://mail.example", 302, "secret", {}, None))
        with self.assertRaises(JMAPError):
            Client(Config("https://mail.example", "user", "secret"), transport).session
        self.assertEqual(len(transport.calls), 1)

    def test_invalid_json_duplicate_keys_and_nonfinite_constants(self):
        for data in (b'{"state":"a","state":"b"}', b'{"x":NaN}', b'{"x":Infinity}', b'[]'):
            with self.subTest(data=data), self.assertRaises(JMAPError):
                Client._decode(data)

    def test_malformed_mutation_outcome_is_unknown(self):
        client = Client(Config("https://mail.example", "user", "secret"), Transport(session_payload(), b"bad JSON"))
        with self.assertRaises(JMAPError) as caught:
            client.set("Email", MAIL, update={"e": {"keywords/$seen": True}})
        self.assertTrue(caught.exception.outcome_unknown)

    def test_server_request_size_limit_is_obeyed_before_mutation(self):
        session = session_payload()
        session["capabilities"]["urn:ietf:params:jmap:core"]["maxSizeRequest"] = 10
        transport = Transport(session)
        with self.assertRaises(JMAPError) as caught:
            Client(Config("https://mail.example", "user", "secret"), transport).set("Email", MAIL, update={"e": {"keywords/$seen": True}})
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(len(transport.calls), 1)

    def test_credential_echo_redacted_without_breaking_json_or_logging(self):
        class Capture(logging.Handler):
            def __init__(self):
                super().__init__()
                self.messages = []

            def emit(self, record):
                self.messages.append(record.getMessage())
        log_capture = Capture()
        logging.getLogger().addHandler(log_capture)
        try:
            for secret in ("top-secret", "}", 'quote"slash\\'):
                config = Config("https://mail.example/session", "user", secret)
                transport = Transport(session_payload(), get_response("Email", [{"id": "e", "subject": secret + config.authorization()}]))
                with patch.dict(os.environ, {"JMAP_SESSION_URL": "https://mail.example/session", "JMAP_USERNAME": "user", "JMAP_SECRET": secret}), patch.object(handlers, "Client", side_effect=lambda cfg: Client(cfg, transport)):
                    result = json.loads(handlers.handler(Context(), "jmap_get_email")({"email_id": "e"}))
                self.assertIn("REDACTED", result["data"]["subject"])
                self.assertNotIn(secret, result["data"]["subject"])
            self.assertEqual(log_capture.messages, [])
        finally:
            logging.getLogger().removeHandler(log_capture)

    def test_disabled_mutation_cannot_be_invoked_directly(self):
        with patch.dict(os.environ, {"JMAP_SESSION_URL": "https://mail.example/session", "JMAP_USERNAME": "user", "JMAP_SECRET": "secret"}), patch.object(handlers, "Client") as client:
            client.return_value.config.enable_mutations = False
            result = json.loads(handlers.handler(Context(), "jmap_send_draft")({"email_id": "e", "identity_id": "i"}))
        self.assertEqual(result["code"], "mutations_disabled")

    def test_no_compose_send_and_delete_is_explicitly_destructive(self):
        schemas = importlib.import_module(PACKAGE + ".schemas")
        self.assertEqual(len(schemas.SCHEMAS), 22)
        self.assertIn("DESTRUCTIVE", schemas.SCHEMAS["jmap_delete_email"]["description"])
        self.assertIn("jmap_delete_email", schemas.CONSEQUENTIAL)
        self.assertNotIn("body", schemas.SCHEMAS["jmap_send_draft"]["parameters"]["properties"])
