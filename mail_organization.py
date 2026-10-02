"""Mailbox metadata and incremental membership/custom keyword changes."""
from . import models
from .errors import JMAPError, malformed
from .session import MAIL

TOOLS = {"jmap_create_mailbox", "jmap_update_mailbox", "jmap_update_email_mailboxes", "jmap_update_email_keywords"}


def keyword(value, *, custom=False):
    if (not isinstance(value, str) or not 1 <= len(value) <= 255 or any(not 33 <= ord(c) <= 126 or c in '(){]%*"\\' for c in value)
            or custom and value.startswith("$")):
        raise JMAPError("invalid_keyword", "Use a valid ASCII keyword; custom labels cannot start with $.")
    return value.lower()


def patch_key(value):
    return value.replace("~", "~0").replace("/", "~1")


def deltas(add, remove, normalizer=lambda value: value):
    added, removed = {normalizer(x) for x in add or []}, {normalizer(x) for x in remove or []}
    if not added and not removed or added & removed:
        raise JMAPError("invalid_arguments", "Specify nonoverlapping additions or removals.")
    return added, removed


class MailOrganization:
    def __init__(self, client):
        self.client = client
        self.account_id = client.config.mail_account_id

    def _get_one(self, kind, identifier, if_in_state=None):
        got = self.client.get(kind, [identifier], MAIL, account_id=self.account_id)
        if not got["list"]:
            raise JMAPError("not_found", "Email or mailbox not found or inaccessible.")
        if if_in_state is not None and got["state"] != if_in_state:
            raise JMAPError("stateMismatch", "Email or mailbox state changed; inspect it before retrying.")
        return got["list"][0], got["state"]

    def _mailbox_right(self, identifier, permission):
        item, _ = self._get_one("Mailbox", identifier)
        if not isinstance(item.get("myRights"), dict) or item["myRights"].get(permission) is not True:
            raise JMAPError("forbidden", "The target mailbox does not grant the required permission.")
        return item

    def _fields(self, values):
        return {{"parent_id": "parentId", "sort_order": "sortOrder", "is_subscribed": "isSubscribed"}.get(key, key): value for key, value in values.items()}

    def _parent(self, parent_id):
        if parent_id is not None:
            self._mailbox_right(parent_id, "mayCreateChild")
        else:
            account = self.client.session.account(MAIL, self.account_id, mutation=True)
            if self.client.session.accounts[account]["accountCapabilities"][MAIL].get("mayCreateTopLevelMailbox") is not True:
                raise JMAPError("forbidden", "The selected account does not permit top-level mailbox creation.")

    def create_mailbox(self, name, parent_id=None, **values):
        self._parent(parent_id)
        result = self.client.set("Mailbox", MAIL, account_id=self.account_id,
                                 create={"mailbox": self._fields(dict(values, name=name, parent_id=parent_id))})
        return models.envelope(result, action="mailbox_created" if result["success"] else "mailbox_creation_failed")

    def update_mailbox(self, mailbox_id, changes, if_in_state=None):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify mailbox fields to change.")
        item, state = self._get_one("Mailbox", mailbox_id, if_in_state)
        if set(changes) - {"is_subscribed"} and (item.get("myRights") or {}).get("mayRename") is not True:
            raise JMAPError("forbidden", "Mailbox rename permission is required for metadata changes.")
        if "parent_id" in changes:
            if changes["parent_id"] == mailbox_id:
                raise JMAPError("invalid_arguments", "A mailbox cannot be its own parent.")
            self._parent(changes["parent_id"])
        result = self.client.set("Mailbox", MAIL, account_id=self.account_id, if_in_state=state,
                                 update={mailbox_id: self._fields(changes)})
        return models.envelope(result, action="mailbox_updated" if result["success"] else "mailbox_update_failed")

    def update_email_mailboxes(self, email_id, add_mailbox_ids=None, remove_mailbox_ids=None, if_in_state=None):
        added, removed = deltas(add_mailbox_ids, remove_mailbox_ids)
        item, state = self._get_one("Email", email_id, if_in_state)
        memberships = item.get("mailboxIds")
        if not isinstance(memberships, dict):
            raise malformed()
        current = {key for key, included in memberships.items() if included is True}
        if not (current | added) - removed:
            raise JMAPError("invalid_arguments", "An email must remain in at least one mailbox.")
        for identifier in added - current:
            self._mailbox_right(identifier, "mayAddItems")
        for identifier in removed & current:
            self._mailbox_right(identifier, "mayRemoveItems")
        changes = {"mailboxIds/" + patch_key(identifier): True for identifier in added - current}
        changes.update({"mailboxIds/" + patch_key(identifier): None for identifier in removed & current})
        if not changes:
            return models.envelope({"success": True}, action="email_memberships_unchanged", email_id=email_id)
        result = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=state, update={email_id: changes})
        return models.envelope(result, action="email_memberships_updated" if result["success"] else "email_membership_update_failed")

    def update_email_keywords(self, email_id, add_keywords=None, remove_keywords=None, if_in_state=None):
        added, removed = deltas(add_keywords, remove_keywords, lambda value: keyword(value, custom=True))
        _, state = self._get_one("Email", email_id, if_in_state)
        changes = {"keywords/" + patch_key(label): True for label in added}
        changes.update({"keywords/" + patch_key(label): None for label in removed})
        result = self.client.set("Email", MAIL, account_id=self.account_id, if_in_state=state, update={email_id: changes})
        return models.envelope(result, action="email_keywords_updated" if result["success"] else "email_keyword_update_failed")
