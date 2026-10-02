"""Bounded binary uploads and verified attachment descriptors for composing mail."""
import mimetypes
import os
from pathlib import Path
import re
import stat

from . import models
from .errors import JMAPError, malformed
from .session import MAIL

MEDIA_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


def filename(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 255
            or any(c in "/\\" or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise JMAPError("invalid_arguments", "Use an attachment filename without paths or control characters.")
    return value


def upload_attachment(client, path, name=None, content_type=None):
    name = filename(name or Path(path).name)
    content_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not MEDIA_TYPE.fullmatch(content_type):
        raise JMAPError("invalid_arguments", "Use a MIME type such as application/pdf.")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise JMAPError("invalid_file", "Upload an explicit regular file, not a symlink or device.")
            if info.st_size > client.config.max_attachment_bytes:
                raise JMAPError("attachment_too_large", "The file exceeds the configured attachment size limit.")
            data = source.read(client.config.max_attachment_bytes + 1)
    except OSError:
        raise JMAPError("invalid_file", "The upload file is unavailable or not an accessible regular file.") from None
    if len(data) > client.config.max_attachment_bytes:
        raise JMAPError("attachment_too_large", "The file exceeds the configured attachment size limit.")
    account = client.session.account(MAIL, client.config.mail_account_id, mutation=True)
    uploaded = client.upload(account, data, content_type)
    return models.envelope({"account_id": account, "blob_id": uploaded["blobId"], "name": name,
                            "type": uploaded["type"], "size": uploaded["size"]}, action="attachment_uploaded", sends_email=False)


def draft_attachments(client, descriptors):
    account = client.session.account(MAIL, client.config.mail_account_id)
    maximum = client.session.accounts[account]["accountCapabilities"][MAIL].get("maxSizeAttachmentsPerEmail")
    if maximum is None:
        maximum = client.config.max_attachment_bytes
    if type(maximum) is not int or maximum < 0:
        raise malformed()
    maximum = min(maximum, client.config.max_attachment_bytes)
    parts, total = [], 0
    for attachment in descriptors or []:
        if attachment["account_id"] != account:
            raise JMAPError("attachment_account_mismatch", "Draft attachments must belong to the selected mail account.")
        filename(attachment["name"])
        if not MEDIA_TYPE.fullmatch(attachment["type"]):
            raise JMAPError("invalid_arguments", "Use a valid attachment MIME type.")
        size = attachment["size"]
        if type(size) is not int or size < 0:
            raise JMAPError("invalid_arguments", "Attachment size must be a nonnegative integer.")
        total += size
        if total > maximum:
            raise JMAPError("attachment_too_large", "The attachment set exceeds the server or configured size limit.")
        parts.append({"blobId": attachment["blob_id"], "name": attachment["name"], "type": attachment["type"], "disposition": "attachment"})
    for attachment in descriptors or []:
        # Verify immutable blob bytes instead of trusting model-supplied sizes.
        if len(client.download(account, attachment["blob_id"])) != attachment["size"]:
            raise JMAPError("attachment_size_mismatch", "Attachment bytes differ from the supplied metadata.")
    return parts
