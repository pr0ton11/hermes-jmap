"""JMAP-only mail reads, normalized and bounded."""
import os
from pathlib import Path
import tempfile

from . import models
from .errors import JMAPError, malformed
from .mail_organization import keyword as validate_keyword
from .session import MAIL
from .time_utils import instant


class Mail:
    def __init__(self, client, attachment_dir=None):
        self.client = client
        self.account_id = client.config.mail_account_id
        self.attachment_dir = attachment_dir

    def _get(self, kind, ids, **kwargs):
        return self.client.get(kind, ids, MAIL, account_id=self.account_id, **kwargs)

    def list_mailboxes(self, **args):
        result = self._get("Mailbox", None)
        maximum = self.client.session.capabilities["urn:ietf:params:jmap:core"].get("maxObjectsInGet", 100)
        complete = type(maximum) is int and len(result["list"]) < maximum and not result["notFound"]
        return models.envelope([models.mailbox(item) for item in result["list"]], state=result["state"], complete=complete,
                               possibly_truncated=not complete, not_found=result["notFound"])

    def _emails(self, ids, **kwargs):
        by_id, missing = {}, []
        size = self.client.session.limit("maxObjectsInGet", 100)
        state = None
        for offset in range(0, len(ids), size):
            result = self._get("Email", ids[offset:offset + size], properties=models.MAIL_SUMMARY, **kwargs)
            if state is not None and result["state"] != state:
                raise JMAPError("state_changed", "Email state changed during retrieval; repeat the read.")
            state = result["state"]
            by_id.update({item["id"]: models.email(item) for item in result["list"]})
            missing.extend(result["notFound"])
        return [by_id[key] for key in ids if key in by_id], missing, state

    @staticmethod
    def _filter(args):
        conditions = []
        for source, target in {"text": "text", "sender": "from", "recipient": "to", "subject": "subject", "before": "before", "after": "after", "mailbox_id": "inMailbox"}.items():
            if source in args:
                if source in {"before", "after"}:
                    instant(args[source])
                conditions.append({target: args[source]})
        for source, keyword, positive in (("unread", "$seen", False), ("flagged", "$flagged", True)):
            if source in args:
                conditions.append({"hasKeyword" if args[source] == positive else "notKeyword": keyword})
        for source, target in (("keyword", "hasKeyword"), ("not_keyword", "notKeyword")):
            if source in args:
                conditions.append({target: validate_keyword(args[source])})
        return {"operator": "AND", "conditions": conditions} if conditions else None

    def list_email(self, **args):
        return self.search_email(**args)

    def search_email(self, **args):
        limit = args.get("limit", 20)
        position = args.get("position", 0)
        request = {"filter": self._filter(args), "sort": [{"property": "receivedAt", "isAscending": False}],
                   "position": position, "limit": limit, "calculateTotal": True}
        result = self.client.call("Email/query", request, MAIL, account_id=self.account_id)
        ids = result.get("ids")
        total = result.get("total")
        actual_position = result.get("position")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) > limit or len(set(ids)) != len(ids):
            raise malformed()
        if type(actual_position) is not int or actual_position < 0 or not isinstance(result.get("queryState"), str):
            raise malformed()
        if total is not None and (type(total) is not int or total < actual_position + len(ids)):
            raise malformed()
        data, missing, state = self._emails(ids)
        more = actual_position + len(ids) < total if total is not None else len(ids) == limit
        return models.envelope(data, not_found=missing, state=state, pagination={
            "position": actual_position, "next_position": actual_position + len(ids) if more else None,
            "has_more": more, "total": total, "query_state": result["queryState"],
        })

    def get_email(self, email_id, include_body=False, max_body_chars=20000):
        props = models.MAIL_SUMMARY + (models.MAIL_BODY if include_body else [])
        extra = {"fetchAllBodyValues": True, "maxBodyValueBytes": max_body_chars * 4} if include_body else {}
        result = self._get("Email", [email_id], properties=props, **extra)
        if not result["list"]:
            raise JMAPError("not_found", "Email not found or not accessible.")
        return models.envelope(models.email(result["list"][0], include_body=include_body, max_body_chars=max_body_chars), state=result["state"])

    def get_thread(self, thread_id, limit=20, position=0):
        result = self._get("Thread", [thread_id])
        if not result["list"]:
            raise JMAPError("not_found", "Thread not found or not accessible.")
        ids = result["list"][0].get("emailIds")
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
            raise malformed()
        data, missing, state = self._emails(ids[position:position + limit])
        more = position + limit < len(ids)
        return models.envelope(data, thread_id=thread_id, not_found=missing, state=state,
                               pagination={"position": position, "total": len(ids), "has_more": more,
                                           "next_position": position + limit if more else None})

    def get_attachment(self, email_id, blob_id):
        email = self.get_email(email_id)["data"]
        matches = [part for part in email["attachments"] if part["blobId"] == blob_id]
        if not matches:
            raise JMAPError("not_found", "The requested blob is not an attachment of this email.")
        part = matches[0]
        size = part.get("size")
        if type(size) is not int or size < 0:
            raise malformed()
        if size > self.client.config.max_attachment_bytes:
            raise JMAPError("attachment_too_large", "The attachment exceeds the configured size limit.")
        account = self.client.session.account(MAIL, self.account_id)
        data = self.client.download(account, blob_id)
        if len(data) != size:
            raise JMAPError("attachment_size_mismatch", "Downloaded attachment size differs from its metadata.")
        directory = Path(self.attachment_dir) if self.attachment_dir else Path(tempfile.mkdtemp(prefix="hermes-jmap-attachments-"))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="attachment-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
        return models.envelope({"path": path, "bytes": len(data), "attachment": part})
