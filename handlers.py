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
from .mail_mutations import MailMutations
from .calendar_mutations import CalendarMutations


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
            if name == "jmap_calendar_availability":
                result = calendar_availability(Calendar(client), **args)
            else:
                owners = {"jmap_calendar_read": Calendar, "jmap_calendar_write": CalendarMutations,
                          "jmap_mail_read": Mail, "jmap_mail_write": MailMutations}
                owner = owners[TOOLSETS[name]](client)
                if name == "jmap_create_event" and "timezone" not in args:
                    args = dict(args, timezone=client.config.timezone)
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
