"""Small synchronous JMAP HTTP client with bounded responses and no automatic retry."""
import json
from http.client import HTTPException
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import JMAPError, malformed
from .session import CORE, Session

MAX_JSON_BYTES = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPTransport:
    def request(self, method, url, headers, body, timeout, max_bytes):
        request = Request(url, data=body, headers=headers, method=method)
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) < 0 or int(declared) > max_bytes:
                        raise JMAPError("response_too_large", "The response exceeds the configured size limit.")
                except ValueError:
                    raise malformed() from None
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise JMAPError("response_too_large", "The response exceeds the configured size limit.")
            return data


class Client:
    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or HTTPTransport()
        self._session = None
        self._lock = threading.RLock()

    def _request(self, method, url, payload=None, *, mutation=False, max_bytes=MAX_JSON_BYTES, stage="jmap_api"):
        self.config.authorize_endpoint(url)
        headers = {"Authorization": self.config.authorization(), "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, allow_nan=False).encode()
            ceiling = self._session.limit("maxSizeRequest", MAX_JSON_BYTES) if self._session is not None else MAX_JSON_BYTES
            if len(body) > ceiling:
                raise JMAPError("request_too_large", "The request exceeds the server or client size limit.")
        try:
            return self.transport.request(method, url, headers, body, self.config.timeout_seconds, max_bytes)
        except HTTPError as exc:
            code = exc.code
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            exc.close()
            details = {"http_status": code, "stage": stage}
            if code in (401, 403):
                raise JMAPError("unauthorized" if code == 401 else "forbidden", "JMAP authentication or authorization failed.", **details) from None
            if code == 429:
                retry = min(int(retry_after), 3600) if retry_after and retry_after.isdecimal() else None
                raise JMAPError("rate_limited", "The server rate limited this operation. Retry later.", retry_after=retry, **details) from None
            if 300 <= code < 400:
                raise JMAPError("redirect_refused", "The endpoint returned a redirect. Configure the direct JMAP Session URL; redirects are not followed with credentials.", **details) from None
            message = "The JMAP HTTP request failed."
            if code == 404:
                message = "The JMAP endpoint was not found. Check the Session URL or discovered API route."
            elif code == 405:
                message = "The endpoint does not accept this HTTP method. Check the JMAP endpoint and proxy routing."
            elif code >= 500:
                message = "The JMAP server or proxy returned an HTTP failure."
            raise JMAPError("http_error", message, outcome_unknown=mutation and code >= 500, **details) from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise JMAPError("network_error", "The JMAP connection failed. Inspect state before retrying a mutation.", outcome_unknown=mutation, stage=stage) from None

    @staticmethod
    def _decode(data):
        def pairs(values):
            output = {}
            for key, value in values:
                if key in output:
                    raise ValueError
                output[key] = value
            return output

        def invalid_constant(value):
            raise ValueError
        try:
            result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise malformed() from None
        if not isinstance(result, dict):
            raise malformed()
        return result

    @property
    def session(self):
        with self._lock:
            if self._session is None:
                self._session = Session.parse(self._decode(self._request("GET", self.config.session_url, stage="session_discovery")), self.config)
            return self._session

    def call(self, method, arguments, capability, *, mutation=False, account_id=None, extra_capabilities=()):
        try:
            return self._call(method, arguments, capability, mutation=mutation, account_id=account_id, extra_capabilities=extra_capabilities)
        except JMAPError as error:
            if mutation and error.code in {"malformed_response", "response_too_large", "serverFail"}:
                error.outcome_unknown = True
            raise

    def _call(self, method, arguments, capability, *, mutation=False, account_id=None, extra_capabilities=()):
        session = self.session
        selected = session.account(capability, account_id, mutation=mutation)
        args = dict(arguments, accountId=selected)
        using = [CORE] if capability == CORE else [CORE, capability]
        for extra in extra_capabilities:
            session.account(extra, selected, mutation=mutation)
            if extra not in using:
                using.append(extra)
        payload = {"using": using, "methodCalls": [[method, args, "request"]]}
        data = self._decode(self._request("POST", session.api_url, payload, mutation=mutation))
        responses = data.get("methodResponses")
        followup_expected = method == "EmailSubmission/set" and bool(args.get("onSuccessUpdateEmail") or args.get("onSuccessDestroyEmail"))
        if not isinstance(responses, list) or not responses or len(responses) > (2 if followup_expected else 1):
            raise malformed()
        response = responses[0]
        followup = None
        if len(responses) == 2:
            second = responses[1]
            if not isinstance(second, list) or len(second) != 3 or second[2] != "request" or second[0] not in {"Email/set", "error"} or not isinstance(second[1], dict):
                raise malformed()
            if second[0] != "error" and second[1].get("accountId") != selected:
                raise malformed()
            followup = second
        if not isinstance(response, list) or len(response) != 3 or response[2] != "request" or not isinstance(response[1], dict):
            raise malformed()
        name, result, _ = response
        if name == "error":
            known = {"forbidden", "accountNotFound", "accountReadOnly", "unknownMethod", "invalidArguments", "requestTooLarge", "serverFail", "stateMismatch", "unsupportedFilter", "unsupportedSort"}
            code = result.get("type")
            raise JMAPError(code if isinstance(code, str) and code in known else "method_error",
                            "The server rejected the JMAP method; check capability, arguments and permissions.")
        if name != method or result.get("accountId") != selected:
            raise malformed()
        if followup_expected and result.get("created") and followup is None:
            raise malformed()
        if followup is not None:
            result = dict(result, _followup_response=followup)
        return result

    def set(self, object_type, capability, *, account_id=None, if_in_state=None, extra_capabilities=(), **operations):
        try:
            return self._set(object_type, capability, account_id=account_id, if_in_state=if_in_state, extra_capabilities=extra_capabilities, **operations)
        except JMAPError as error:
            if error.code in {"malformed_response", "response_too_large", "serverFail"}:
                error.outcome_unknown = True
            raise

    def _set(self, object_type, capability, *, account_id=None, if_in_state=None, extra_capabilities=(), **operations):
        args = dict(operations)
        if if_in_state is not None:
            args["ifInState"] = if_in_state
        result = self.call(object_type + "/set", args, capability, mutation=True,
                           account_id=account_id, extra_capabilities=extra_capabilities)
        for key in ("oldState", "newState"):
            if not isinstance(result.get(key), str):
                raise malformed()
        outcome = {"old_state": result["oldState"], "new_state": result["newState"], "created": {}, "updated": [], "destroyed": [], "errors": {}}
        pairs = (("create", "created", "notCreated"), ("update", "updated", "notUpdated"), ("destroy", "destroyed", "notDestroyed"))
        for requested, success_key, error_key in pairs:
            expected = set(args.get(requested) or [])
            success = result.get(success_key) or ([] if requested == "destroy" else {})
            failure = result.get(error_key) or {}
            if not isinstance(failure, dict) or not isinstance(success, list if requested == "destroy" else dict):
                raise malformed()
            if set(success) & set(failure) or set(success) | set(failure) != expected:
                raise malformed()
            if requested == "destroy" and len(success) != len(set(success)):
                raise malformed()
            if requested == "update" and any(value is not None and not isinstance(value, dict) for value in success.values()):
                raise malformed()
            if requested == "create":
                for key, value in success.items():
                    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
                        raise malformed()
                    outcome["created"][key] = value["id"]
            else:
                outcome[success_key] = list(success)
            for key, value in failure.items():
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    raise malformed()
                code = value["type"]
                safe = {"forbidden", "notFound", "invalidProperties", "overQuota", "tooLarge", "stateMismatch", "invalidEmail", "invalidRecipients", "noRecipients", "notPermittedFrom", "serverFail", "blobNotFound"}
                outcome["errors"][key] = {"code": code if code in safe else "object_error", "operation": requested}
        outcome["success"] = not outcome["errors"]
        followup = result.get("_followup_response")
        if followup is not None:
            value = followup[1]
            if followup[0] == "error":
                outcome["email_cleanup"] = {"success": False, "code": "method_error"}
            else:
                failures = value.get("notUpdated") or {}
                destroyed_failures = value.get("notDestroyed") or {}
                updated = value.get("updated") or {}
                destroyed = value.get("destroyed") or []
                if not isinstance(failures, dict) or not isinstance(destroyed_failures, dict) or not isinstance(updated, dict) or not isinstance(destroyed, list):
                    raise malformed()
                expected_updates = set()
                for identifier in operations.get("onSuccessUpdateEmail") or {}:
                    if identifier.startswith("#"):
                        ref = identifier[1:]
                        if ref in outcome["created"]:
                            expected_updates.add(operations["create"][ref]["emailId"])
                    else:
                        expected_updates.add(identifier)
                if set(updated) & set(failures) or set(updated) | set(failures) != expected_updates:
                    raise malformed()
                if any(not isinstance(x, str) for x in destroyed) or any(not isinstance(x, dict) for x in failures.values()):
                    raise malformed()
                outcome["email_cleanup"] = {"success": not failures and not destroyed_failures,
                                             "updated": list(updated), "destroyed": destroyed,
                                             "failed_ids": list(failures) + list(destroyed_failures)}
        return outcome

    def get(self, object_type, ids, capability, *, properties=None, account_id=None, **extra):
        session = self.session
        limit = session.limit("maxObjectsInGet", 100)
        if ids is not None and len(ids) > limit:
            raise JMAPError("too_many_ids", "Reduce the requested IDs to the server get limit.")
        args = {"ids": ids, **extra}
        if properties is not None:
            args["properties"] = properties
        result = self.call(object_type + "/get", args, capability, account_id=account_id)
        values, missing = result.get("list"), result.get("notFound")
        if not isinstance(values, list) or not isinstance(missing, list) or not isinstance(result.get("state"), str):
            raise malformed()
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in values) or any(not isinstance(item, str) for item in missing):
            raise malformed()
        returned = [item["id"] for item in values]
        if len(returned) != len(set(returned)) or set(returned) & set(missing):
            raise malformed()
        if ids is not None and (set(returned) | set(missing) != set(ids)):
            raise malformed()
        return result

    def download(self, account_id, blob_id, name="attachment"):
        template = self.session.download_url
        for key, value in {"accountId": account_id, "blobId": blob_id, "name": name, "type": "application/octet-stream"}.items():
            template = template.replace("{" + key + "}", quote(value, safe=""))
        if "{" in template or "}" in template:
            raise malformed()
        return self._request("GET", template, max_bytes=self.config.max_attachment_bytes, stage="attachment_download")
