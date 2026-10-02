"""JMAP Contacts reads and narrow, state-guarded address-book/contact writes."""
import uuid

from . import models
from .collection_reads import get_all, query_objects
from .errors import JMAPError
from .session import CONTACTS

BOOK_PROPERTIES = ["id", "name", "description", "sortOrder", "isDefault", "isSubscribed", "myRights"]
SUMMARY_PROPERTIES = ["id", "uid", "addressBookIds", "name", "emails", "phones", "organizations"]


def contact_fields(values, *, patch=False):
    result = {}
    for key, value in values.items():
        if key == "full_name":
            if patch:
                result["name/full"] = value
            else:
                result["name"] = {"full": value}
        else:
            entries = [{"note": note} for note in value] if key == "notes" else value
            result[key] = {str(uuid.uuid4()): dict(entry) for entry in entries}
    return result


class Contacts:
    def __init__(self, client):
        self.client = client
        self.account_id = client.config.contacts_account_id

    def _get_one(self, kind, identifier, if_in_state=None, properties=None):
        got = self.client.get(kind, [identifier], CONTACTS, account_id=self.account_id, properties=properties)
        if not got["list"]:
            raise JMAPError("not_found", "Contact or address book not found or inaccessible.")
        if if_in_state is not None and got["state"] != if_in_state:
            raise JMAPError("stateMismatch", "Contact or address book state changed; inspect it before retrying.")
        return got["list"][0], got["state"]

    def list_address_books(self):
        return get_all(self.client, "AddressBook", CONTACTS, self.account_id, BOOK_PROPERTIES)

    def get_address_book(self, address_book_id):
        item, state = self._get_one("AddressBook", address_book_id, properties=BOOK_PROPERTIES)
        return models.envelope(item, state=state)

    def get_contact(self, contact_id):
        item, state = self._get_one("ContactCard", contact_id)
        return models.envelope(item, state=state)

    def list_contacts(self, **args):
        return self.search_contacts(**args)

    def search_contacts(self, address_book_id=None, limit=20, position=0, **matches):
        filters = dict(matches)
        if address_book_id is not None:
            filters["inAddressBook"] = address_book_id
        return query_objects(self.client, "ContactCard", CONTACTS, self.account_id, filters or None,
                             limit=limit, position=position, properties=SUMMARY_PROPERTIES)

    def _book_rights(self, identifier):
        item, _ = self._get_one("AddressBook", identifier, properties=BOOK_PROPERTIES)
        if not isinstance(item.get("myRights"), dict) or item["myRights"].get("mayWrite") is not True:
            raise JMAPError("forbidden", "Address book write permission is required.")

    def _contact_for_write(self, identifier, if_in_state):
        item, state = self._get_one("ContactCard", identifier, if_in_state)
        memberships = item.get("addressBookIds")
        if not isinstance(memberships, dict) or not any(value is True for value in memberships.values()):
            raise JMAPError("invalid_contact", "Contact has no accessible address book membership.")
        for book, included in memberships.items():
            if included is True:
                self._book_rights(book)
        return item, state

    def create_address_book(self, name, **values):
        account = self.client.session.account(CONTACTS, self.account_id, mutation=True)
        if self.client.session.accounts[account]["accountCapabilities"][CONTACTS].get("mayCreateAddressBook") is not True:
            raise JMAPError("forbidden", "The selected account does not permit address book creation.")
        self._book_name(name)
        mapped = self._book_fields(dict(values, name=name))
        result = self.client.set("AddressBook", CONTACTS, account_id=account, create={"book": mapped})
        return models.envelope(result, action="address_book_created" if result["success"] else "address_book_creation_failed")

    @staticmethod
    def _book_name(name):
        if not name or len(name.encode("utf-8")) > 255:
            raise JMAPError("invalid_arguments", "Address book names must fit 255 UTF-8 bytes.")

    @staticmethod
    def _book_fields(values):
        return {{"sort_order": "sortOrder", "is_subscribed": "isSubscribed"}.get(key, key): value for key, value in values.items()}

    def update_address_book(self, address_book_id, changes, if_in_state=None):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify address book fields to change.")
        _, state = self._get_one("AddressBook", address_book_id, if_in_state, BOOK_PROPERTIES)
        if "name" in changes:
            self._book_name(changes["name"])
        result = self.client.set("AddressBook", CONTACTS, account_id=self.account_id, if_in_state=state,
                                 update={address_book_id: self._book_fields(changes)})
        return models.envelope(result, action="address_book_updated" if result["success"] else "address_book_update_failed")

    def create_contact(self, address_book_id, full_name, **fields):
        self._book_rights(address_book_id)
        item = {"@type": "Card", "version": "1.0", "uid": str(uuid.uuid4()), "kind": "individual",
                "addressBookIds": {address_book_id: True}, **contact_fields(dict(fields, full_name=full_name))}
        result = self.client.set("ContactCard", CONTACTS, account_id=self.account_id, create={"contact": item})
        return models.envelope(result, action="contact_created" if result["success"] else "contact_creation_failed")

    def update_contact(self, contact_id, changes, if_in_state=None):
        if not changes:
            raise JMAPError("invalid_arguments", "Specify contact fields to change.")
        old, state = self._contact_for_write(contact_id, if_in_state)
        fields = contact_fields(changes, patch=True)
        if "name/full" in fields and not isinstance(old.get("name"), dict):
            fields["name"] = {"full": fields.pop("name/full")}
        result = self.client.set("ContactCard", CONTACTS, account_id=self.account_id, if_in_state=state,
                                 update={contact_id: fields})
        return models.envelope(result, action="contact_updated" if result["success"] else "contact_update_failed")

    def delete_contact(self, contact_id, if_in_state=None):
        _, state = self._contact_for_write(contact_id, if_in_state)
        result = self.client.set("ContactCard", CONTACTS, account_id=self.account_id, if_in_state=state, destroy=[contact_id])
        return models.envelope(result, contact_id=contact_id, permanent=True,
                               action="contact_deleted" if result["success"] else "contact_deletion_failed")
