"""Draft editing and sending are separate JMAP operations; MIME edits replace drafts."""
import copy
from email.utils import parseaddr

from . import models
from .errors import JMAPError, malformed
from .session import MAIL, SUBMISSION

DRAFT_PROPERTIES = ["id", "keywords", "mailboxIds", "from", "to", "cc", "bcc", "replyTo", "subject", "bodyStructure", "attachments"]


def validate_addresses(values):
    for key in ("to", "cc", "bcc"):
        for address in values.get(key) or []:
            email = address["email"]
            try:
                if any(c in email for c in "\r\n") or parseaddr(email)[1] != email or "@" not in email:
                    raise ValueError
            except (ValueError, TypeError):
                raise JMAPError("invalid_address", "Use structured addr-spec email addresses without header text.") from None


def body_structure(body, attachments):
    text = {"type": "text/plain", "charset": "utf-8", "partId": "draft-text"}
    parts = [text]
    for attachment in attachments:
        if not isinstance(attachment, dict) or not isinstance(attachment.get("blobId"), str):
            raise malformed()
        parts.append({key: attachment[key] for key in ("blobId", "type", "name", "disposition", "cid", "charset") if key in attachment})
    structure = {"type": "multipart/mixed", "subParts": parts} if attachments else text
    return {"bodyStructure": structure, "bodyValues": {"draft-text": {"value": body, "isTruncated": False, "isEncodingProblem": False}}}


def reusable_structure(part):
    if not isinstance(part, dict):
        raise malformed()
    result = {key: value for key, value in part.items() if key in {"blobId", "type", "name", "charset", "disposition", "cid", "language", "location"} and value is not None}
    if "subParts" in part and part["subParts"] is not None:
        if not isinstance(part["subParts"], list):
            raise malformed()
        result["subParts"] = [reusable_structure(child) for child in part["subParts"]]
    elif not isinstance(part.get("blobId"), str):
        raise JMAPError("draft_not_editable", "A draft body part cannot be preserved without a blob reference.")
    return result


class MailMutations:
    def __init__(self, client):
        self.client = client
        self.account_id = client.config.mail_account_id

    def _draft(self, email_id, if_in_state=None):
        result = self.client.get("Email", [email_id], MAIL, properties=DRAFT_PROPERTIES, account_id=self.account_id)
        if not result["list"]:
            raise JMAPError("not_found", "Draft not found or not accessible.")
        item = result["list"][0]
        if not isinstance(item.get("keywords"), dict) or item["keywords"].get("$draft") is not True:
            raise JMAPError("not_a_draft", "This operation requires an existing unsent draft.")
        if if_in_state is not None and result["state"] != if_in_state:
            raise JMAPError("stateMismatch", "Draft state changed; inspect it before retrying.")
        return item, result["state"]

    def _identity(self, identity_id=None):
        account = self.client.session.account(MAIL, self.account_id)
        result = self.client.get("Identity", [identity_id] if identity_id else None, SUBMISSION, account_id=account)
        candidates = result["list"]
        if not identity_id:
            matching = [item for item in candidates if item.get("email", "").casefold() == self.client.config.username.casefold()]
            if len(matching) == 1:
                candidates = matching
        if len(candidates) != 1 or not isinstance(candidates[0].get("email"), str):
            raise JMAPError("identity_selection", "Choose an accessible sending identity ID for this account.")
        return candidates[0], account

    def create_draft(self, to, subject, body, cc=None, bcc=None, identity_id=None):
        validate_addresses({"to": to, "cc": cc, "bcc": bcc})
        identity, account = self._identity(identity_id)
        mailboxes = self.client.get("Mailbox", None, MAIL, account_id=account)
        drafts = [item["id"] for item in mailboxes["list"] if item.get("role") == "drafts"]
        if len(drafts) != 1:
            raise JMAPError("drafts_mailbox", "An accessible drafts-role mailbox is required.")
        item = {"mailboxIds": {drafts[0]: True}, "keywords": {"$draft": True}, "from": [{"email": identity["email"], "name": identity.get("name", "")}],
                "to": to, "cc": cc or [], "bcc": bcc or [], "subject": subject, **body_structure(body, [])}
        result = self.client.set("Email", MAIL, account_id=account, create={"draft": item})
        return models.envelope(result, action="draft_created", sends_email=False)

    def update_draft(self, email_id, if_in_state=None, **changes):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify at least one draft field to change.")
        validate_addresses(changes)
        old, state = self._draft(email_id, if_in_state)
        replacement = {key: copy.deepcopy(old[key]) for key in ("mailboxIds", "keywords", "from", "to", "cc", "bcc", "replyTo", "subject") if key in old}
        if "body" in changes:
            replacement.update(body_structure(changes.pop("body"), old.get("attachments", [])))
        else:
            replacement["bodyStructure"] = reusable_structure(old.get("bodyStructure"))
        replacement.update(changes)
        created = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=state, create={"replacement": replacement})
        if not created["success"]:
            return models.envelope(created, action="draft_replacement_failed", old_email_id=email_id, sends_email=False)
        new_id = created["created"]["replacement"]
        try:
            cleanup = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=created["new_state"], destroy=[email_id])
        except JMAPError as error:
            return models.envelope({"success": False, "cleanup_error": error.as_dict()}, action="draft_replaced_cleanup_incomplete", old_email_id=email_id, email_id=new_id, sends_email=False)
        return models.envelope(cleanup, action="draft_replaced" if cleanup["success"] else "draft_replaced_cleanup_incomplete", old_email_id=email_id, email_id=new_id, sends_email=False)

    def _patch(self, email_id, changes, if_in_state=None):
        result = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=if_in_state, update={email_id: changes})
        return models.envelope(result, action="email_updated")

    def move_email(self, email_id, mailbox_id, if_in_state=None):
        target = self.client.get("Mailbox", [mailbox_id], MAIL, account_id=self.account_id)
        if not target["list"]:
            raise JMAPError("not_found", "Destination mailbox not found or inaccessible.")
        # Move means single destination; flag tools below preserve other keyword state.
        return self._patch(email_id, {"mailboxIds": {mailbox_id: True}}, if_in_state)

    def mark_read(self, email_id, read, if_in_state=None):
        return self._patch(email_id, {"keywords/$seen": True if read else None}, if_in_state)

    def set_flagged(self, email_id, flagged, if_in_state=None):
        return self._patch(email_id, {"keywords/$flagged": True if flagged else None}, if_in_state)

    def delete_email(self, email_id, if_in_state=None):
        existing = self.client.get("Email", [email_id], MAIL, account_id=self.account_id, properties=["id"])
        if not existing["list"]:
            raise JMAPError("not_found", "Email not found or not accessible; nothing was deleted.")
        state = existing["state"]
        if if_in_state is not None and if_in_state != state:
            raise JMAPError("stateMismatch", "Email state changed; inspect it before deletion.")
        outcome = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=state, destroy=[email_id])
        return models.envelope(outcome, action="email_deleted" if outcome["success"] else "email_deletion_failed",
                               email_id=email_id, permanent=True)

    def send_draft(self, email_id, identity_id, if_in_state=None):
        draft, state = self._draft(email_id, if_in_state)
        identity, account = self._identity(identity_id)
        addresses = draft.get("from")
        if not isinstance(addresses, list) or len(addresses) != 1 or not isinstance(addresses[0], dict) or addresses[0].get("email", "").casefold() != identity["email"].casefold():
            raise JMAPError("identity_mismatch", "The draft From address must match the selected sending identity.")
        # Guard the Email state again immediately before submission. Submission state is
        # a separate type and cannot be guarded by an Email state token.
        self._draft(email_id, state)
        result = self.client.set("EmailSubmission", SUBMISSION, account_id=account, extra_capabilities=(MAIL,),
                                 create={"submission": {"emailId": email_id, "identityId": identity_id}},
                                 onSuccessUpdateEmail={"#submission": {"keywords/$draft": None, "keywords/$seen": True}})
        return models.envelope(result, action="email_submitted", email_id=email_id,
                               note="Submission acceptance is not proof of final delivery.")
