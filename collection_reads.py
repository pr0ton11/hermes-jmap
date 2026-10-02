"""Bounded collection reads shared by contacts and identity discovery."""
from . import models
from .errors import JMAPError, malformed


def get_all(client, kind, capability, account_id=None, properties=None):
    got = client.get(kind, None, capability, account_id=account_id, properties=properties)
    complete = len(got["list"]) < client.session.limit("maxObjectsInGet", 100) and not got["notFound"]
    return models.envelope(got["list"], state=got["state"], complete=complete,
                           possibly_truncated=not complete, not_found=got["notFound"])


def query_objects(client, kind, capability, account_id, filters, *, limit=20, position=0, properties=None):
    result = client.call(kind + "/query", {"filter": filters, "limit": limit, "position": position,
                                          "calculateTotal": True}, capability, account_id=account_id)
    ids, total = result.get("ids"), result.get("total")
    if (not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids))
            or len(ids) > limit or type(result.get("position")) is not int or result["position"] != position
            or not isinstance(result.get("queryState"), str)
            or total is not None and (type(total) is not int or total < 0 or ids and total < position + len(ids))):
        raise malformed()
    values, missing, state = {}, [], None
    batch = client.session.limit("maxObjectsInGet", 100)
    for offset in range(0, len(ids), batch):
        got = client.get(kind, ids[offset:offset + batch], capability, account_id=account_id, properties=properties)
        if state is not None and state != got["state"]:
            raise JMAPError("state_changed", "Collection state changed during retrieval; repeat the read.")
        state = got["state"]
        values.update({item["id"]: item for item in got["list"]})
        missing.extend(got["notFound"])
    more = position + len(ids) < total if total is not None else len(ids) == limit
    if more and not ids:
        raise malformed()
    return models.envelope([values[key] for key in ids if key in values], state=state, not_found=missing,
                           pagination={"position": position, "next_position": position + len(ids) if more else None,
                                       "has_more": more, "total": total, "query_state": result["queryState"]})
