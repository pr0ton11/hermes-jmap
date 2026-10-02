"""Identity discovery and deterministic resolution without inventing addresses."""
from .collection_reads import get_all
from .errors import JMAPError
from .session import CALENDARS, MAIL, SUBMISSION


def mail_identities(client):
    account = client.session.account(MAIL, client.config.mail_account_id)
    return get_all(client, "Identity", SUBMISSION, account, ["id", "name", "email"])


def participant_identities(client):
    return get_all(client, "ParticipantIdentity", CALENDARS, client.config.calendar_account_id,
                   ["id", "name", "calendarAddress", "isDefault"])


def participant_identity(client, identity_id=None):
    if identity_id:
        got = client.get("ParticipantIdentity", [identity_id], CALENDARS, account_id=client.config.calendar_account_id)
        candidates = got["list"]
    else:
        got = participant_identities(client)
        if not got["complete"]:
            raise JMAPError("identity_selection", "Identity discovery is incomplete; specify a participant identity ID.")
        candidates = got["data"]
        defaults = [item for item in candidates if item.get("isDefault") is True]
        if defaults:
            candidates = defaults
    if len(candidates) != 1 or not isinstance(candidates[0].get("calendarAddress"), str) or not candidates[0]["calendarAddress"]:
        raise JMAPError("identity_selection", "Choose one accessible calendar participant identity ID.")
    return candidates[0]
