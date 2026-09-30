"""Safe errors: never carry remote response bodies or transport exception text."""


class JMAPError(Exception):
    def __init__(self, code, message, *, outcome_unknown=False, retry_after=None):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after

    def as_dict(self):
        result = {"error": str(self), "code": self.code}
        if self.outcome_unknown:
            result["outcome_unknown"] = True
        if self.retry_after is not None:
            result["retry_after_seconds"] = self.retry_after
        return result


def malformed():
    return JMAPError("malformed_response", "The server returned an invalid JMAP response.")
