"""Domain errors mapped to HTTP responses at the API boundary."""


class WeightingError(Exception):
    """Base class for expected domain errors."""

    status_code = 400
    code = "weighting_error"

    def __init__(self, message: str, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(WeightingError):
    status_code = 400
    code = "invalid_payload"


class MissingTableError(WeightingError):
    status_code = 409
    code = "missing_table"


class MissingTargetsError(WeightingError):
    status_code = 409
    code = "missing_group_targets"


class NotReadyError(WeightingError):
    status_code = 409
    code = "not_ready"


class NoResultError(WeightingError):
    status_code = 409
    code = "result_unavailable"


class JobRunningError(WeightingError):
    status_code = 409
    code = "weighting_in_progress"
