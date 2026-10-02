"""Read-only capability diagnostics; never return authentication or endpoint values."""
from . import models
from .errors import JMAPError
from .session import MAIL, SUBMISSION, CALENDARS, CALENDAR_PARSE, CONTACTS


def status(client):
    result = {"enable_mutations": client.config.enable_mutations, "timezone": client.config.timezone,
              "write_setup": "Set plugins.entries.hermes-jmap.settings.enable_mutations to true and restart Hermes.",
              "capabilities": {}, "accounts": {}}
    try:
        session = client.session
    except JMAPError as error:
        result["discovery_error"] = error.as_dict()
        return models.envelope(result)
    for name, capability, override in (("mail", MAIL, client.config.mail_account_id),
                                      ("submission", SUBMISSION, client.config.mail_account_id),
                                      ("calendars", CALENDARS, client.config.calendar_account_id),
                                      ("calendar_parse", CALENDAR_PARSE, client.config.calendar_account_id),
                                      ("contacts", CONTACTS, client.config.contacts_account_id)):
        result["capabilities"][name] = capability in session.capabilities
        try:
            if name == "submission":
                override = session.account(MAIL, client.config.mail_account_id)
            elif name == "calendar_parse":
                override = session.account(CALENDARS, client.config.calendar_account_id)
            identifier = session.account(capability, override)
            result["accounts"][name] = {"account_id": identifier, "is_read_only": session.accounts[identifier]["isReadOnly"],
                                        "writes_configured": client.config.enable_mutations,
                                        "object_permissions": "Check the target object's rights; account writability alone does not grant access."}
        except JMAPError as error:
            result["accounts"][name] = {"selection_error": error.as_dict()}
    return models.envelope(result)
