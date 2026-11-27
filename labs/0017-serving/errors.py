"""Error taxonomy for the serving lab.

The distinction that matters for retries is not "exception vs return code" but
"is repeating this action safe and likely to help?"  The agent maps every
failure into one of the buckets below before a retry policy sees it.
"""


class ServiceError(Exception):
    """Base class for errors that map to an HTTP response."""

    code = "service_error"
    http_status = 500

    def __init__(self, message="", detail=None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.detail = detail or {}


class BadRequest(ServiceError):
    code = "bad_request"
    http_status = 400


class MissingIdempotencyKey(BadRequest):
    code = "missing_idempotency_key"


class IdempotencyConflict(ServiceError):
    code = "idempotency_conflict"
    http_status = 422


class RunNotFound(ServiceError):
    code = "run_not_found"
    http_status = 404


class RunNotTerminal(ServiceError):
    code = "run_not_terminal"
    http_status = 409


class RetryableError(Exception):
    """Transient failure: timeout, 429/503, connection reset.

    Only this bucket is allowed to enter a retry loop.
    """


class NonRetryableError(Exception):
    """Permanent failure: validation, auth, policy denial, 4xx.

    Retrying it wastes budget and can turn a clear error into a slow one.
    """


class RetryBudgetExhausted(RetryableError):
    """The retry token bucket is empty; fail fast instead of retrying."""


class DeadlineExceeded(Exception):
    """The run-level deadline expired before the work finished."""


class RunCancelled(Exception):
    """The caller asked to stop, or the server is draining.

    It is not an error in the retry sense: the run is paused for a restart or
    marked cancelled, depending on who asked.
    """


class FatalStepError(NonRetryableError):
    """A step failed in a way that makes the rest of the run meaningless."""
