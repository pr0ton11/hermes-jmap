import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from .extension_support import PACKAGE, client, requests, session, JMAPError
from .test_mail import get_response, response
from .test_mutations import set_response, draft

MailWorkflows = importlib.import_module(PACKAGE + ".mail_workflows").MailWorkflows
MailOrganization = importlib.import_module(PACKAGE + ".mail_organization").MailOrganization
Mail = importlib.import_module(PACKAGE + ".mail").Mail
schemas = importlib.import_module(PACKAGE + ".schemas")


def identities():
    return get_response("Identity", [{"id": "identity", "email": "user@example.org"}, {"id": "alias", "email": "alias@example.org"}])


def mailbox(identifier="inbox", **rights):
    return get_response("Mailbox", [{"id": identifier, "myRights": rights}])


def drafts():
    return get_response("Mailbox", [{"id": "drafts", "role": "drafts"}])


def attachment(**fields):
    return {"account_id": "a", "blob_id": "blob", "name": "file.pdf", "type": "application/pdf", "size": 3, **fields}


class CompositionTests(unittest.TestCase):
    def test_reply_all_honors_reply_to_excludes_self_and_bcc_and_threads(self):
        original = {"id": "mail", "from": [{"email": "sender@example.org"}], "replyTo": [{"email": "reply@example.org"}],
            "to": [{"email": "user@example.org"}, {"email": "to@example.org"}, {"email": "REPLY@example.org"}],
            "cc": [{"email": "alias@example.org"}, {"email": "cc@example.org"}, {"email": "TO@example.org"}],
            "bcc": [{"email": "hidden@example.org"}], "subject": "RE: Topic", "messageId": ["original@example.org"], "references": ["parent@example.org"]}
        c, transport = client(identities(), get_response("Email", [original]), drafts(), set_response("Email", created={"draft": {"id": "new"}}))
        result = MailWorkflows(c).create_reply_draft("mail", "Reply", reply_all=True)
        item = requests(transport, "Email/set")[0]["create"]["draft"]
        self.assertEqual(item["to"], [{"email": "reply@example.org"}])
        self.assertEqual(item["cc"], [{"email": "to@example.org"}, {"email": "cc@example.org"}])
        self.assertEqual(item["bcc"], [])
        self.assertEqual(item["subject"], "RE: Topic")
        self.assertEqual(item["inReplyTo"], ["original@example.org"])
        self.assertEqual(item["references"], ["parent@example.org", "original@example.org"])
        self.assertEqual(result["email_id"], "new")
        self.assertEqual(result["identity_id"], "identity")
        self.assertFalse(requests(transport, "EmailSubmission/set"))

    def test_reply_from_fallback_missing_message_id_and_no_self_recipient(self):
        c, transport = client(identities(), get_response("Email", [{"id": "mail", "from": [{"email": "sender@example.org"}], "subject": "Topic"}]),
                              drafts(), set_response("Email", created={"draft": {"id": "new"}}))
        MailWorkflows(c).create_reply_draft("mail", "Reply")
        item = requests(transport, "Email/set")[0]["create"]["draft"]
        self.assertEqual(item["to"], [{"email": "sender@example.org"}])
        self.assertEqual(item["subject"], "Re: Topic")
        self.assertNotIn("inReplyTo", item)
        c, transport = client(identities(), get_response("Email", [{"id": "mail", "from": [{"email": "user@example.org"}]}]))
        with self.assertRaises(JMAPError) as caught:
            MailWorkflows(c).create_reply_draft("mail", "Reply")
        self.assertEqual(caught.exception.code, "noRecipients")
        self.assertFalse(requests(transport, "Email/set"))

    def test_reply_header_injection_and_ambiguous_identity_never_create(self):
        c, transport = client(identities(), get_response("Email", [{"id": "mail", "from": [{"email": "sender@example.org"}], "messageId": ["x\r\nInjected: true"]}]))
        with self.assertRaises(JMAPError):
            MailWorkflows(c).create_reply_draft("mail", "Reply")
        self.assertFalse(requests(transport, "Email/set"))
        c, transport = client(get_response("Identity", [{"id": "one", "email": "one@example.org"}, {"id": "two", "email": "two@example.org"}]))
        with self.assertRaises(JMAPError):
            MailWorkflows(c).create_reply_draft("mail", "Reply")
        self.assertFalse(requests(transport, "Email/set"))

    def test_incomplete_identities_cannot_claim_all_self_aliases(self):
        data = session()
        data["capabilities"]["urn:ietf:params:jmap:core"]["maxObjectsInGet"] = 2
        c, transport = client(identities(), payload=data)
        with self.assertRaises(JMAPError) as caught:
            MailWorkflows(c).create_reply_draft("mail", "Reply", reply_all=True)
        self.assertEqual(caught.exception.code, "identity_selection")
        self.assertFalse(requests(transport, "Email/set"))

    def test_forward_attaches_original_mime_bytes_without_rewriting(self):
        raw = b"From: original@example.org\r\nContent-Type: multipart/mixed\r\n\r\noriginal MIME"
        c, transport = client(get_response("Identity", [{"id": "identity", "email": "user@example.org"}]),
            get_response("Email", [{"id": "mail", "blobId": "original", "size": len(raw), "subject": "Topic"}]), raw,
            drafts(), set_response("Email", created={"draft": {"id": "forward"}}))
        result = MailWorkflows(c).create_forward_draft("mail", [{"email": "friend@example.org"}], "Forwarding")
        item = requests(transport, "Email/set")[0]["create"]["draft"]
        self.assertEqual(item["subject"], "Fwd: Topic")
        self.assertEqual(item["bodyStructure"]["subParts"][1], {"blobId": "original", "name": "forwarded.eml", "type": "message/rfc822", "disposition": "attachment"})
        self.assertEqual(result["forwarding_mode"], "attached_original_message")
        self.assertFalse(requests(transport, "EmailSubmission/set"))

    def test_create_draft_with_attachment_verifies_blob_and_builds_mime(self):
        c, transport = client(get_response("Identity", [{"id": "identity", "email": "user@example.org"}]), b"pdf", drafts(), set_response("Email", created={"draft": {"id": "new"}}))
        MailWorkflows(c).create_draft([{ "email": "friend@example.org"}], "File", "Attached", attachments=[attachment()])
        item = requests(transport, "Email/set")[0]["create"]["draft"]
        self.assertEqual(item["bodyStructure"]["subParts"][1]["blobId"], "blob")
        self.assertEqual(item["bodyValues"]["draft-text"]["value"], "Attached")

    def test_wrong_account_size_mismatch_and_aggregate_limit_do_not_create(self):
        for descriptors, responses, payload in (([attachment(account_id="b")], [], None),
                                               ([attachment()], [b"wrong"], None),
                                               ([attachment(), attachment(blob_id="two")], [], self.limited_session(5))):
            c, transport = client(get_response("Identity", [{"id": "identity", "email": "user@example.org"}]), *responses, payload=payload)
            # A batch exceeding the aggregate limit must be rejected before any upload/download.
            if len(descriptors) == 2:
                transport.responses.append(b"pdf")
            with self.assertRaises(JMAPError):
                MailWorkflows(c).create_draft([{ "email": "friend@example.org"}], "File", "Attached", attachments=descriptors)
            self.assertFalse(requests(transport, "Email/set"))

    @staticmethod
    def limited_session(maximum):
        data = session()
        data["accounts"]["a"]["accountCapabilities"]["urn:ietf:params:jmap:mail"]["maxSizeAttachmentsPerEmail"] = maximum
        return data

    def test_replacement_preserves_thread_headers_and_body_alternatives(self):
        old = draft()
        old.update(inReplyTo=["original@example.org"], references=["parent@example.org"],
                   bodyStructure={"type": "multipart/mixed", "subParts": [{"type": "multipart/alternative", "subParts": [
                        {"type": "text/plain", "blobId": "plain"}, {"type": "text/html", "blobId": "html"}]},
                        {"type": "application/pdf", "blobId": "attachment", "name": "old.pdf"}]})
        c, transport = client(get_response("Email", [old]), b"pdf", set_response("Email", created={"replacement": {"id": "new"}}), set_response("Email", destroyed=["draft"]))
        result = MailWorkflows(c).update_draft("draft", attachments=[attachment()])
        replacement = requests(transport, "Email/set")[0]["create"]["replacement"]
        self.assertEqual(replacement["inReplyTo"], old["inReplyTo"])
        self.assertEqual(replacement["references"], old["references"])
        self.assertIn('"plain"', json.dumps(replacement["bodyStructure"]))
        self.assertIn('"html"', json.dumps(replacement["bodyStructure"]))
        self.assertNotIn('"blobId": "attachment"', json.dumps(replacement["bodyStructure"]))
        self.assertEqual(result["email_id"], "new")


class UploadTests(unittest.TestCase):
    def test_regular_file_upload_uses_discovery_binary_headers_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.pdf"
            source.write_bytes(b"pdf")
            c, transport = client({"accountId": "a", "blobId": "blob", "size": 3, "type": "application/pdf"})
            result = MailWorkflows(c).upload_attachment(str(source))
        self.assertEqual(result["data"], attachment())
        self.assertEqual(transport.calls[1][1], "https://mail.example/upload/a")
        self.assertEqual(transport.calls[1][2]["Content-Type"], "application/pdf")
        self.assertEqual(transport.calls[1][3], b"pdf")
        self.assertFalse(result["sends_email"])

    def test_upload_size_filename_type_and_nonregular_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "file.pdf"
            source.write_bytes(b"pdf")
            link = Path(directory) / "link"
            link.symlink_to(source)
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo)
            for args in ({"path": str(link)}, {"path": str(fifo)}, {"path": directory},
                         {"path": str(source), "name": "../file.pdf"}, {"path": str(source), "content_type": "text/plain\r\nInjected: true"}):
                c, transport = client()
                with self.assertRaises(JMAPError):
                    MailWorkflows(c).upload_attachment(**args)
                self.assertEqual(transport.calls, [])
            c, transport = client(max_attachment_bytes=2)
            with self.assertRaises(JMAPError):
                MailWorkflows(c).upload_attachment(str(source))
            self.assertEqual(transport.calls, [])

    def test_advertised_upload_limit_is_obeyed(self):
        data = session()
        data["capabilities"]["urn:ietf:params:jmap:core"]["maxSizeUpload"] = 2
        c, transport = client(payload=data)
        with self.assertRaises(JMAPError):
            c.upload("a", b"pdf", "application/pdf")
        self.assertEqual(len(transport.calls), 1)

    def test_malformed_and_uncertain_upload_do_not_retry(self):
        for result in ({"accountId": "other", "blobId": "blob", "size": 3, "type": "application/pdf"}, URLError("private-secret")):
            c, transport = client(result)
            with self.assertRaises(JMAPError) as caught:
                c.upload("a", b"pdf", "application/pdf")
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(len(transport.calls), 2)
            self.assertNotIn("private-secret", str(caught.exception))


class OrganizationTests(unittest.TestCase):
    def test_create_and_update_mailbox_respect_parent_permissions_and_state(self):
        c, transport = client(mailbox("parent", mayCreateChild=True), set_response("Mailbox", created={"mailbox": {"id": "new"}}))
        MailOrganization(c).create_mailbox("Projects", parent_id="parent", sort_order=2)
        self.assertEqual(requests(transport, "Mailbox/set")[0]["create"]["mailbox"], {"name": "Projects", "parentId": "parent", "sortOrder": 2})
        c, transport = client(mailbox("box", mayRename=True), set_response("Mailbox", updated={"box": None}))
        MailOrganization(c).update_mailbox("box", {"name": "New", "is_subscribed": True}, if_in_state="s2")
        request = requests(transport, "Mailbox/set")[0]
        self.assertEqual(request["update"], {"box": {"name": "New", "isSubscribed": True}})
        self.assertEqual(request["ifInState"], "s2")

    def test_mailbox_permissions_stale_state_and_self_parent_prevent_updates(self):
        for item, changes, state in (({}, {"name": "New"}, None), ({"mayRename": True}, {"name": "New"}, "old"),
                                     ({"mayRename": True}, {"parent_id": "box"}, None)):
            c, transport = client(mailbox("box", **item))
            with self.assertRaises(JMAPError):
                MailOrganization(c).update_mailbox("box", changes, state)
            self.assertFalse(requests(transport, "Mailbox/set"))

    def test_incremental_memberships_preserve_unrelated_mailboxes(self):
        c, transport = client(get_response("Email", [{"id": "mail", "mailboxIds": {"old": True, "keep": True}}]),
                              mailbox("new/~", mayAddItems=True), mailbox("old", mayRemoveItems=True), set_response("Email", updated={"mail": None}))
        MailOrganization(c).update_email_mailboxes("mail", ["new/~"], ["old"])
        request = requests(transport, "Email/set")[0]
        self.assertEqual(request["update"], {"mail": {"mailboxIds/new~1~0": True, "mailboxIds/old": None}})
        self.assertEqual(request["ifInState"], "s2")

    def test_empty_membership_or_overlapping_changes_are_refused(self):
        for added, removed, responses in (([], ["only"], [get_response("Email", [{"id": "mail", "mailboxIds": {"only": True}}])]),
                                         (["box"], ["box"], []), ([], [], [])):
            c, transport = client(*responses)
            with self.assertRaises(JMAPError):
                MailOrganization(c).update_email_mailboxes("mail", added, removed)
            self.assertFalse(requests(transport, "Email/set"))

    def test_custom_keywords_are_normalized_escaped_and_preserve_system_flags(self):
        c, transport = client(get_response("Email", [{"id": "mail", "keywords": {"$seen": True, "$flagged": True, "old": True}}]),
                              set_response("Email", updated={"mail": None}))
        MailOrganization(c).update_email_keywords("mail", ["Projects/Work~"], ["OLD"])
        self.assertEqual(requests(transport, "Email/set")[0]["update"], {"mail": {"keywords/projects~1work~0": True, "keywords/old": None}})

    def test_reserved_invalid_and_conflicting_keywords_never_mutate(self):
        for added, removed in ((["$draft"], []), (["with space"], []), (["x"], ["X"]), (["non-ascii-é"], [])):
            c, transport = client()
            with self.assertRaises(JMAPError):
                MailOrganization(c).update_email_keywords("mail", added, removed)
            self.assertFalse(requests(transport, "Email/set"))

    def test_keyword_search_combines_with_existing_filters(self):
        c, transport = client(response("Email/query", ids=[], position=0, total=0, queryState="q"))
        Mail(c).search_email(keyword="WORK", not_keyword="old", unread=True)
        self.assertEqual(requests(transport, "Email/query")[0]["filter"]["conditions"], [
            {"notKeyword": "$seen"}, {"hasKeyword": "work"}, {"notKeyword": "old"}])
