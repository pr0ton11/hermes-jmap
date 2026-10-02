"""Unsent reply/forward drafts built from discovered identities and verified mail."""
import re

from .attachments import draft_attachments, upload_attachment
from .errors import JMAPError, malformed
from .identities import mail_identities
from .mail_mutations import MailMutations, validate_addresses
from .session import MAIL


def recipients(addresses, excluded):
    validate_addresses({"to": addresses})
    result = []
    for address in addresses:
        key = address["email"].casefold()
        if key not in excluded:
            excluded.add(key)
            result.append({field: address[field] for field in ("email", "name") if field in address})
    return result


def message_ids(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item or any(c in item for c in "\r\n") for item in value):
        raise malformed()
    return value


class MailWorkflows(MailMutations):
    def upload_attachment(self, path, name=None, content_type=None):
        return upload_attachment(self.client, path, name, content_type)

    def create_reply_draft(self, email_id, body, reply_all=False, identity_id=None, attachments=None):
        identities = mail_identities(self.client)
        if not identities["complete"]:
            raise JMAPError("identity_selection", "Sending identity discovery is incomplete; resolve your identities before replying.")
        choices = identities["data"]
        if identity_id:
            choices = [item for item in choices if item["id"] == identity_id]
        else:
            matching = [item for item in choices if item.get("email", "").casefold() == self.client.config.username.casefold()]
            if len(matching) == 1:
                choices = matching
        if len(choices) != 1 or not isinstance(choices[0].get("email"), str):
            raise JMAPError("identity_selection", "Choose one accessible sending identity ID.")
        identity = choices[0]
        got = self.client.get("Email", [email_id], MAIL, account_id=self.account_id,
                              properties=["id", "replyTo", "from", "to", "cc", "subject", "messageId", "references"])
        if not got["list"]:
            raise JMAPError("not_found", "The source email was not found or is inaccessible.")
        email = got["list"][0]
        excluded = {item["email"].casefold() for item in identities["data"] if isinstance(item.get("email"), str)}
        to = recipients(email.get("replyTo") or email.get("from") or [], excluded)
        cc = []
        if reply_all:
            cc = recipients([*(email.get("to") or []), *(email.get("cc") or [])], excluded)
        if not to and cc:
            to, cc = cc, []
        if not to:
            raise JMAPError("noRecipients", "The reply has no recipient after excluding your identities.")
        original_ids = message_ids(email.get("messageId") or [])
        references = message_ids(email.get("references") or [])
        headers = {"inReplyTo": original_ids, "references": list(dict.fromkeys([*references, *original_ids]))} if original_ids else {}
        subject = email.get("subject") or ""
        if not isinstance(subject, str):
            raise malformed()
        if not re.match(r"^re:", subject, re.IGNORECASE):
            subject = "Re: " + subject
        if len(subject) > 998:
            raise JMAPError("invalid_arguments", "The reply subject exceeds the supported length.")
        account = self.client.session.account(MAIL, self.account_id)
        parts = draft_attachments(self.client, attachments)
        result = self._create_draft(identity, account, to, subject, body, cc=cc, parts=parts, headers=headers)
        result.update(source_email_id=email_id, reply_all=reply_all)
        return result

    def create_forward_draft(self, email_id, to, body, cc=None, bcc=None, identity_id=None, attachments=None):
        identity, account = self._identity(identity_id)
        got = self.client.get("Email", [email_id], MAIL, account_id=account, properties=["id", "blobId", "size", "subject"])
        if not got["list"]:
            raise JMAPError("not_found", "The source email was not found or is inaccessible.")
        email = got["list"][0]
        if not isinstance(email.get("blobId"), str) or type(email.get("size")) is not int or email["size"] < 0:
            raise malformed()
        original = {"account_id": account, "blob_id": email["blobId"], "size": email["size"],
                    "name": "forwarded.eml", "type": "message/rfc822"}
        parts = draft_attachments(self.client, [original, *(attachments or [])])
        subject = email.get("subject") or ""
        if not isinstance(subject, str):
            raise malformed()
        if not re.match(r"^(fw|fwd):", subject, re.IGNORECASE):
            subject = "Fwd: " + subject
        if len(subject) > 998:
            raise JMAPError("invalid_arguments", "The forward subject exceeds the supported length.")
        result = self._create_draft(identity, account, to, subject, body, cc, bcc, parts)
        result.update(source_email_id=email_id, forwarding_mode="attached_original_message")
        return result
