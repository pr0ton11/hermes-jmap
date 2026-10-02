"""Hermes adapter: validation, lazy operations and sanitized JSON-string results."""
import json
import os

from .client import Client
from .config import Config
from .errors import JMAPError
from .mail import Mail
from .calendar_ops import Calendar
from .availability import calendar_availability
from .schemas import MUTATIONS, TOOLSETS, validate
from .mail_workflows import MailWorkflows
from .mail_organization import MailOrganization, TOOLS as ORGANIZATION_TOOLS
from .calendar_mutations import CalendarMutations
from .contacts import Contacts
from .invitations import Invitations
from .identities import mail_identities, participant_identities
from .status import status


def redact(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    return value


def handler(ctx, name):
    def invoke(args, **kwargs):
        try:
            validate(name, args)
            client = Client(Config.from_plugin(ctx, os.environ))
            if name in MUTATIONS and not client.config.enable_mutations:
                raise JMAPError("mutations_disabled", "Enable this plugin's enable_mutations setting before using write tools.")
            if name == "jmap_status":
                result = status(client)
            elif name == "jmap_list_identities":
                result = mail_identities(client)
            elif name == "jmap_list_participant_identities":
                result = participant_identities(client)
            elif name == "jmap_calendar_availability":
                result = calendar_availability(Calendar(client), **args)
            else:
                owners = {"jmap_calendar_read": Calendar, "jmap_calendar_write": CalendarMutations,
                          "jmap_mail_read": Mail, "jmap_mail_write": MailWorkflows,
                          "jmap_contacts_read": Contacts, "jmap_contacts_write": Contacts}
                kind = owners[TOOLSETS[name]]
                if name in ORGANIZATION_TOOLS:
                    kind = MailOrganization
                elif name in {"jmap_preview_calendar_invitation", "jmap_import_calendar_invitation"}:
                    kind = Invitations
                owner = kind(client)
                result = getattr(owner, name.removeprefix("jmap_"))(**args)
            # A hostile server can echo authentication strings in otherwise normal
            # content. Redact string values before serialization to preserve JSON.
            authorization = client.config.authorization()
            sensitive = (authorization, authorization.split(" ", 1)[1], client.config.secret)
            return json.dumps(redact(result, sensitive), ensure_ascii=False, allow_nan=False)
        except JMAPError as error:
            return json.dumps(error.as_dict())
        except Exception:
            result = {"error": "JMAP operation failed; no diagnostic content is exposed.", "code": "internal_error"}
            if name in MUTATIONS:
                result["outcome_unknown"] = True
            return json.dumps(result)
    return invoke
