"""Synthetic accounts and request inspection for extension behavior tests."""
import importlib
import json

from .support import load_plugin
from .test_foundation import Transport
from .test_mutations import mutation_session

PACKAGE = load_plugin()
Config = importlib.import_module(PACKAGE + ".config").Config
Client = importlib.import_module(PACKAGE + ".client").Client
JMAPError = importlib.import_module(PACKAGE + ".errors").JMAPError
CONTACTS = "urn:ietf:params:jmap:contacts"
PARSE = "urn:ietf:params:jmap:calendars:parse"


def session():
    data = mutation_session()
    for cap in (CONTACTS, PARSE):
        data["capabilities"][cap] = {}
        data["accounts"]["a"]["accountCapabilities"][cap] = {}
        data["primaryAccounts"][cap] = "a"
    data["accounts"]["a"]["accountCapabilities"][CONTACTS]["mayCreateAddressBook"] = True
    data["accounts"]["a"]["accountCapabilities"]["urn:ietf:params:jmap:mail"]["mayCreateTopLevelMailbox"] = True
    return data


def client(*responses, payload=None, **settings):
    transport = Transport(payload or session(), *responses)
    settings = dict(enable_mutations=True, **settings) if "enable_mutations" not in settings else settings
    return Client(Config("https://mail.example/session", "user@example.org", "private-secret", **settings), transport), transport


def requests(transport, method):
    return [call[1] for request in transport.calls if request[3] is not None and request[2].get("Content-Type") == "application/json"
            for call in json.loads(request[3])["methodCalls"] if call[0] == method]
