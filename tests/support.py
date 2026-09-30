"""Import standalone directory plugins using the same package semantics as Hermes."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def load_plugin():
    name = "hermes_jmap_test_plugin"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return name


def session_payload():
    return {
        "username": "fixture-user@example.org",
        "capabilities": {"urn:ietf:params:jmap:core": {"maxObjectsInGet": 100},
                         "urn:ietf:params:jmap:mail": {}},
        "accounts": {"a": {"name": "Personal", "isPersonal": True, "isReadOnly": False,
                           "accountCapabilities": {"urn:ietf:params:jmap:mail": {}}}},
        "primaryAccounts": {"urn:ietf:params:jmap:mail": "a"},
        "apiUrl": "https://mail.example/api", "uploadUrl": "https://mail.example/upload/{accountId}",
        "downloadUrl": "https://mail.example/download/{accountId}/{blobId}/{name}?type={type}",
        "eventSourceUrl": "https://mail.example/events", "state": "s1",
    }
