"""Allowlisted, bounded model-facing data. Remote content never becomes instructions."""
from .errors import malformed

MAIL_SUMMARY = ["id", "threadId", "mailboxIds", "keywords", "size", "receivedAt", "sentAt", "from", "to", "cc", "bcc", "replyTo", "subject", "preview", "hasAttachment", "attachments"]
MAIL_BODY = ["textBody", "htmlBody", "bodyValues", "bodyStructure"]


def envelope(data, **metadata):
    return {"data": data, "content_trust": "untrusted", **metadata}


def mailbox(item):
    return {key: item.get(key) for key in ("id", "name", "parentId", "role", "sortOrder", "totalEmails", "unreadEmails", "totalThreads", "unreadThreads", "myRights")}


def attachment(part):
    if not isinstance(part, dict) or not isinstance(part.get("blobId"), str):
        raise malformed()
    return {key: part.get(key) for key in ("partId", "blobId", "name", "type", "size", "disposition", "cid")}


def email(item, *, include_body=False, max_body_chars=20000):
    result = {key: item.get(key) for key in MAIL_SUMMARY if key != "attachments"}
    parts = item.get("attachments", [])
    if not isinstance(parts, list):
        raise malformed()
    result["attachments"] = [attachment(part) for part in parts]
    if include_body:
        values = item.get("bodyValues", {})
        if not isinstance(values, dict):
            raise malformed()
        result["bodies"] = []
        remaining = max_body_chars
        for kind in ("textBody", "htmlBody"):
            body_parts = item.get(kind, [])
            if not isinstance(body_parts, list):
                raise malformed()
            for part in body_parts:
                if not isinstance(part, dict) or not isinstance(part.get("partId"), str):
                    raise malformed()
                value = values.get(part["partId"])
                if not isinstance(value, dict) or not isinstance(value.get("value"), str):
                    raise malformed()
                text = value["value"]
                clipped = text[:remaining]
                remaining -= len(clipped)
                result["bodies"].append({"type": part.get("type"), "value": clipped,
                                         "truncated": bool(value.get("isTruncated")) or len(clipped) < len(text),
                                         "encoding_problem": bool(value.get("isEncodingProblem"))})
    return result
