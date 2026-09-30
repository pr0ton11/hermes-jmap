"""Plugin-scoped settings and private authentication; no Hermes dependency."""
from dataclasses import dataclass, field
import base64
import ipaddress
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import JMAPError


def origin(url):
    try:
        if not isinstance(url, str) or any(ord(character) < 33 for character in url):
            raise ValueError
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if not host or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError
        if parts.scheme == "http":
            if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        elif parts.scheme != "https":
            raise ValueError
        return parts.scheme, host.lower(), port or (443 if parts.scheme == "https" else 80)
    except (ValueError, TypeError):
        raise JMAPError("unsafe_endpoint", "Use HTTPS endpoints without embedded credentials or fragments.") from None


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise JMAPError("invalid_configuration", f"Invalid {label} setting.")
    return value


@dataclass(frozen=True)
class Config:
    session_url: str = field(repr=False)
    username: str = field(repr=False)
    secret: str = field(repr=False)
    auth_type: str = "basic"
    mail_account_id: str | None = None
    calendar_account_id: str | None = None
    timezone: str = "Europe/Zurich"
    timeout_seconds: int = 20
    max_attachment_bytes: int = 10 * 1024 * 1024
    trusted_origins: tuple = ()
    enable_mutations: bool = False

    def __post_init__(self):
        origin(self.session_url)
        if not self.username or not self.secret or self.auth_type not in {"basic", "bearer"}:
            raise JMAPError("invalid_configuration", "Set JMAP_USERNAME, JMAP_SECRET and a supported auth_type.")
        if any(c in self.secret + self.username for c in "\r\n"):
            raise JMAPError("invalid_configuration", "Authentication configuration contains invalid characters.")
        if self.auth_type == "basic" and ":" in self.username:
            raise JMAPError("invalid_configuration", "Basic authentication usernames cannot contain a colon.")
        for identifier in (self.mail_account_id, self.calendar_account_id):
            if identifier is not None and (not isinstance(identifier, str) or not identifier):
                raise JMAPError("invalid_configuration", "Account overrides must be nonempty account ID strings.")
        integer(self.timeout_seconds, 1, 120, "timeout_seconds")
        integer(self.max_attachment_bytes, 1, 100 * 1024 * 1024, "max_attachment_bytes")
        if type(self.enable_mutations) is not bool:
            raise JMAPError("invalid_configuration", "enable_mutations must be a boolean.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise JMAPError("invalid_configuration", "Choose a valid IANA timezone.") from None
        for endpoint in self.trusted_origins:
            origin(endpoint)

    def authorize_endpoint(self, url):
        allowed = {origin(self.session_url), *(origin(item) for item in self.trusted_origins)}
        if origin(url) not in allowed:
            raise JMAPError("untrusted_endpoint", "A discovered endpoint origin requires explicit trusted_origins configuration.")

    def authorization(self):
        if self.auth_type == "bearer":
            return "Bearer " + self.secret
        return "Basic " + base64.b64encode(f"{self.username}:{self.secret}".encode()).decode()

    @classmethod
    def from_plugin(cls, ctx, env):
        settings = {key: ctx.get_config(key, default=default) for key, default in {
            "auth_type": "basic", "mail_account_id": None, "calendar_account_id": None,
            "timezone": "Europe/Zurich", "timeout_seconds": 20,
            "max_attachment_bytes": 10 * 1024 * 1024, "trusted_origins": [],
            "enable_mutations": False,
        }.items()}
        if not isinstance(settings["trusted_origins"], list) or not all(isinstance(x, str) for x in settings["trusted_origins"]):
            raise JMAPError("invalid_configuration", "trusted_origins must be a list of endpoint origins.")
        settings["trusted_origins"] = tuple(settings["trusted_origins"])
        return cls(env.get("JMAP_SESSION_URL", ""), env.get("JMAP_USERNAME", ""),
                   env.get("JMAP_SECRET", ""), **settings)
