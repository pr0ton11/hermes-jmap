"""JMAP Session discovery validation and deterministic capability/account selection."""
from dataclasses import dataclass, field

from .errors import JMAPError, malformed

CORE = "urn:ietf:params:jmap:core"
MAIL = "urn:ietf:params:jmap:mail"
SUBMISSION = "urn:ietf:params:jmap:submission"
CALENDARS = "urn:ietf:params:jmap:calendars"
AVAILABILITY = "urn:ietf:params:jmap:principals:availability"


@dataclass(frozen=True)
class Session:
    api_url: str
    upload_url: str
    download_url: str
    event_source_url: str
    accounts: dict = field(repr=False)
    primary_accounts: dict
    capabilities: dict
    state: str

    @classmethod
    def parse(cls, data, config):
        if not isinstance(data, dict):
            raise malformed()
        fields = ("apiUrl", "uploadUrl", "downloadUrl", "eventSourceUrl", "state")
        if any(not isinstance(data.get(key), str) or not data[key] for key in fields):
            raise malformed()
        if not isinstance(data.get("username"), str):
            raise malformed()
        for key in fields[:4]:
            config.authorize_endpoint(data[key])
        caps, accounts, primary = (data.get(k) for k in ("capabilities", "accounts", "primaryAccounts"))
        if not all(isinstance(x, dict) for x in (caps, accounts, primary)) or CORE not in caps:
            raise malformed()
        if any(not isinstance(k, str) or not isinstance(v, dict) for k, v in caps.items()):
            raise malformed()
        for account_id, account in accounts.items():
            if not isinstance(account_id, str) or not isinstance(account, dict):
                raise malformed()
            acaps = account.get("accountCapabilities")
            if not isinstance(acaps, dict) or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in acaps.items()):
                raise malformed()
            if type(account.get("isReadOnly")) is not bool or type(account.get("isPersonal")) is not bool or not isinstance(account.get("name"), str):
                raise malformed()
        if any(not isinstance(k, str) or not isinstance(v, str) or v not in accounts for k, v in primary.items()):
            raise malformed()
        return cls(*(data[k] for k in fields[:4]), accounts, primary, caps, data["state"])

    def account(self, capability, explicit=None, *, mutation=False):
        if capability not in self.capabilities:
            raise JMAPError("unsupported_capability", "The server does not advertise the required JMAP capability.")
        candidates = [key for key, value in self.accounts.items() if capability in value["accountCapabilities"]]
        selected = explicit or self.primary_accounts.get(capability)
        if selected is None and len(candidates) == 1:
            selected = candidates[0]
        if selected not in candidates:
            raise JMAPError("account_selection", "Configure an account ID with the required capability; account selection is ambiguous or invalid.")
        if mutation and self.accounts[selected]["isReadOnly"]:
            raise JMAPError("read_only_account", "The selected account is read-only.")
        return selected

    def limit(self, key, default):
        value = self.capabilities[CORE].get(key, default)
        if type(value) is not int or value <= 0:
            raise malformed()
        return min(value, default)
