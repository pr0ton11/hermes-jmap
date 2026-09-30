import base64
import copy
import importlib
import json
import unittest
from urllib.error import HTTPError, URLError

from .support import load_plugin, session_payload

PACKAGE = load_plugin()
Config = importlib.import_module(PACKAGE + ".config").Config
Session = importlib.import_module(PACKAGE + ".session").Session
MAIL = importlib.import_module(PACKAGE + ".session").MAIL
Client = importlib.import_module(PACKAGE + ".client").Client
JMAPError = importlib.import_module(PACKAGE + ".errors").JMAPError


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args):
        self.calls.append(args)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response).encode() if isinstance(response, dict) else response


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("https://mail.example/session", "user", "private-secret")

    def test_basic_and_bearer_authentication(self):
        self.assertEqual(self.config.authorization(), "Basic " + base64.b64encode(b"user:private-secret").decode())
        self.assertEqual(Config("https://mail.example/session", "user", "token", auth_type="bearer").authorization(), "Bearer token")
        self.assertNotIn("private-secret", repr(self.config))
        self.assertNotIn("user", repr(self.config))

    def test_discovery_is_lazy_and_cached(self):
        transport = Transport(session_payload())
        client = Client(self.config, transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.session.account(MAIL), "a")
        self.assertIs(client.session, client.session)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(client.session.download_url, session_payload()["downloadUrl"])

    def test_account_selection_and_read_only(self):
        data = session_payload()
        data["accounts"]["b"] = copy.deepcopy(data["accounts"]["a"])
        data["primaryAccounts"] = {}
        session = Session.parse(data, self.config)
        with self.assertRaises(JMAPError):
            session.account(MAIL)
        self.assertEqual(session.account(MAIL, "b"), "b")
        data["accounts"]["b"]["isReadOnly"] = True
        with self.assertRaises(JMAPError) as caught:
            Session.parse(data, self.config).account(MAIL, "b", mutation=True)
        self.assertEqual(caught.exception.code, "read_only_account")

    def test_capability_required_at_both_levels(self):
        for target in ("capabilities", "accountCapabilities"):
            data = session_payload()
            (data[target] if target == "capabilities" else data["accounts"]["a"][target]).pop(MAIL)
            with self.assertRaises(JMAPError):
                Session.parse(data, self.config).account(MAIL)

    def test_malformed_session(self):
        for field in ("apiUrl", "accounts", "capabilities", "state", "primaryAccounts"):
            data = session_payload()
            data[field] = None
            with self.subTest(field=field), self.assertRaises(JMAPError):
                Session.parse(data, self.config)

    def test_untrusted_discovered_origins_are_rejected_before_use(self):
        data = session_payload()
        data["apiUrl"] = "https://attacker.example/api"
        transport = Transport(data)
        with self.assertRaises(JMAPError):
            Client(self.config, transport).session
        self.assertEqual(len(transport.calls), 1)
        trusted = Config("https://mail.example/session", "user", "secret", trusted_origins=("https://attacker.example",))
        self.assertEqual(Session.parse(data, trusted).api_url, data["apiUrl"])

    def test_unsafe_configuration(self):
        for url in ("http://mail.example/session", "https://user:password@mail.example/session", "https://mail.example/#token", "file:///tmp/mail"):
            with self.subTest(url=url), self.assertRaises(JMAPError):
                Config(url, "user", "secret")
        Config("http://127.0.0.1/session", "user", "secret")
        with self.assertRaises(JMAPError):
            Config("https://mail.example", "user", "secret\r\nHeader: bad")

    def test_method_error_never_exposes_remote_message(self):
        transport = Transport(session_payload(), {"methodResponses": [["error", {"type": "forbidden", "description": "private-secret"}, "request"]]})
        with self.assertRaises(JMAPError) as caught:
            Client(self.config, transport).call("Email/get", {}, MAIL)
        self.assertEqual(caught.exception.code, "forbidden")
        self.assertNotIn("private-secret", json.dumps(caught.exception.as_dict()))

    def test_response_tag_method_account_and_json(self):
        for response in (b"bad json", {"methodResponses": []},
                         {"methodResponses": [["Email/get", {"accountId": "b"}, "request"]]},
                         {"methodResponses": [["Email/get", {"accountId": "a"}, "other"]]},
                         {"methodResponses": [["Mailbox/get", {"accountId": "a"}, "request"]]}):
            with self.subTest(response=response), self.assertRaises(JMAPError):
                Client(self.config, Transport(session_payload(), response)).call("Email/get", {}, MAIL)

    def test_http_authorization_rate_limit_and_network_failures(self):
        for code in (401, 403, 429, 500):
            transport = Transport(session_payload(), HTTPError("https://mail.example/private-secret", code, "private-secret", {"Retry-After": "10"}, None))
            with self.subTest(code=code), self.assertRaises(JMAPError) as caught:
                Client(self.config, transport).call("Email/set", {}, MAIL, mutation=True)
            self.assertNotIn("private-secret", str(caught.exception))
            if code == 429:
                self.assertEqual(caught.exception.retry_after, 10)
        transport = Transport(session_payload(), URLError("private-secret"))
        with self.assertRaises(JMAPError) as caught:
            Client(self.config, transport).call("Email/set", {}, MAIL, mutation=True)
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(len(transport.calls), 2)

    def test_get_integrity_and_limit(self):
        valid = {"methodResponses": [["Email/get", {"accountId": "a", "state": "s2", "list": [{"id": "x"}], "notFound": ["y"]}, "request"]]}
        client = Client(self.config, Transport(session_payload(), valid))
        self.assertEqual(client.get("Email", ["x", "y"], MAIL)["notFound"], ["y"])
        invalid = copy.deepcopy(valid)
        invalid["methodResponses"][0][1]["notFound"] = []
        with self.assertRaises(JMAPError):
            Client(self.config, Transport(session_payload(), invalid)).get("Email", ["x", "y"], MAIL)
        with self.assertRaises(JMAPError):
            Client(self.config, Transport(session_payload())).get("Email", [str(x) for x in range(101)], MAIL)

    def test_download_template_encodes_ids(self):
        transport = Transport(session_payload(), b"binary")
        result = Client(self.config, transport).download("a", "x/y", "../bad")
        self.assertEqual(result, b"binary")
        self.assertIn("x%2Fy/..%2Fbad", transport.calls[1][1])


if __name__ == "__main__":
    unittest.main()
