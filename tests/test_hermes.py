"""Optional actual Hermes runtime tests; set HERMES_SOURCE to an inspected checkout."""
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from .support import ROOT, session_payload
from .test_foundation import Transport
from .test_mail import get_response


@unittest.skipUnless(os.environ.get("HERMES_SOURCE"), "HERMES_SOURCE is unset")
class HermesTests(unittest.TestCase):
    def test_real_loader_registration_and_registry_invocation(self):
        sys.path.insert(0, os.environ["HERMES_SOURCE"])
        from hermes_cli.plugin_dev import _doctor_runtime, doctor_plugin
        from tools.registry import registry
        report = doctor_plugin(ROOT)
        self.assertTrue(report.ok, report.format_text())
        with _doctor_runtime(ROOT) as runtime:
            self.assertIn("jmap_list_mailboxes", runtime.registered_tools)
            entry = registry.get_entry("jmap_list_mailboxes")
            module = importlib.import_module(entry.handler.__module__)
            transport = Transport(session_payload(), get_response("Mailbox", [{"id": "m", "name": "Inbox", "role": "inbox"}]))
            actual_client = module.Client
            with patch.dict(os.environ, {"JMAP_SESSION_URL": "https://mail.example/session", "JMAP_USERNAME": "user", "JMAP_SECRET": "test-only"}), patch.object(module, "Client", side_effect=lambda cfg: actual_client(cfg, transport)):
                result = registry.dispatch("jmap_list_mailboxes", {}, task_id="runtime-test")
            self.assertIsInstance(result, str)
            self.assertEqual(json.loads(result)["data"][0]["role"], "inbox")
            self.assertEqual(len(transport.calls), 2)

    def test_actual_approval_gate_denial_acceptance_and_failure(self):
        sys.path.insert(0, os.environ["HERMES_SOURCE"])
        from hermes_cli.plugin_dev import _doctor_runtime
        from hermes_cli.plugins import _PreToolCallDirective, _resolve_block_from_details
        with _doctor_runtime(ROOT) as runtime:
            from tools import approval
            from .support import load_plugin
            consequential = importlib.import_module(load_plugin() + ".schemas").CONSEQUENTIAL
            for name in consequential:
                directives = runtime.manager.invoke_hook("pre_tool_call", tool_name=name, args={})
                directive = next(value for value in directives if isinstance(value, dict) and value.get("action") == "approve")
                details = _PreToolCallDirective(action=directive["action"], message=directive["message"], rule_key=directive["rule_key"])
                for approved in (False, True):
                    with patch.object(approval, "request_tool_approval", return_value={"approved": approved}) as gate:
                        blocked = _resolve_block_from_details(details, name)
                        self.assertEqual(blocked is None, approved)
                        gate.assert_called_once()
                with patch.object(approval, "request_tool_approval", side_effect=RuntimeError("unavailable")):
                    self.assertIn("BLOCKED", _resolve_block_from_details(details, name))

    def test_user_install_is_disabled_until_explicitly_enabled(self):
        sys.path.insert(0, os.environ["HERMES_SOURCE"])
        from hermes_cli.plugins import PluginManager
        from tools.registry import registry
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / "plugins" / "hermes-jmap"
            shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(".git", "tests", "docs", "__pycache__"))
            bundled = home / "empty-bundled"
            bundled.mkdir()
            env = {"HERMES_HOME": str(home), "HERMES_BUNDLED_PLUGINS": str(bundled), "HERMES_ENABLE_PROJECT_PLUGINS": "0",
                   "JMAP_SESSION_URL": "https://mail.example/session", "JMAP_USERNAME": "test-only", "JMAP_SECRET": "test-only"}
            with patch.dict(os.environ, env):
                for enabled, writable in ((False, False), (True, False), (True, True)):
                    (home / "config.yaml").write_text("plugins:\n  enabled: " + ("[hermes-jmap]" if enabled else "[]") +
                        "\n  entries:\n    hermes-jmap:\n      settings:\n        enable_mutations: " + str(writable).lower() + "\n", encoding="utf-8")
                    manager = PluginManager()
                    try:
                        manager.discover_and_load()
                        self.assertEqual(manager._plugins["hermes-jmap"].enabled, enabled)
                        if enabled:
                            self.assertIsNotNone(registry.get_entry("jmap_delete_email"))
                            for name in ("jmap_delete_email", "jmap_create_contact", "jmap_create_reply_draft", "jmap_respond_to_event"):
                                self.assertEqual(registry.get_entry(name).check_fn(), writable)
                            module = importlib.import_module(registry.get_entry("jmap_status").handler.__module__)
                            from .extension_support import session
                            actual_client = module.Client
                            transports = iter([Transport(session()), Transport(session(), get_response("AddressBook", [{"id": "book", "name": "People"}]))])
                            with patch.object(module, "Client", side_effect=lambda cfg: actual_client(cfg, next(transports))):
                                result = json.loads(registry.dispatch("jmap_status", {}, task_id="runtime-test"))
                                self.assertEqual(result["data"]["enable_mutations"], writable)
                                self.assertTrue(result["data"]["capabilities"]["contacts"])
                                books = json.loads(registry.dispatch("jmap_list_address_books", {}, task_id="runtime-test"))
                                self.assertEqual(books["data"][0]["id"], "book")
                    finally:
                        manager.unload()
