__all__ = [
    "ConflictError",
    "HangerError",
    "ModelError",
    "RequestError",
    "ReviewRejectedError",
    "SearchUnavailableError",
]


class HangerError(Exception):
    """An actionable failure at a Hanger boundary."""

    code = "error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConflictError(HangerError):
    """The current document no longer matches the saved proposal."""

    code = "source_changed"
    retryable = True


class RequestError(HangerError):
    """The request identity or payload is invalid."""

    code = "invalid_request"


class ReviewRejectedError(HangerError):
    """The content review rejected a generated proposal."""

    code = "review_rejected"
    retryable = True

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues) or "The review did not approve the change.")


class SearchUnavailableError(HangerError):
    """The managed search engine cannot answer right now."""

    code = "search_unavailable"
    retryable = True


class ModelError(HangerError):
    """The model command failed or returned output outside its contract."""

    code = "model_error"
    retryable = True
