__all__ = [
    "ConflictError",
    "MarkchainError",
    "ModelError",
    "RequestError",
    "ReviewRejectedError",
    "SearchUnavailableError",
]


class MarkchainError(Exception):
    """An actionable failure at a Markchain boundary."""

    code = "error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConflictError(MarkchainError):
    """The current document no longer matches the saved proposal."""

    code = "source_changed"
    retryable = True


class RequestError(MarkchainError):
    """The request identity or payload is invalid."""

    code = "invalid_request"


class ReviewRejectedError(MarkchainError):
    """The content review rejected a generated proposal."""

    code = "review_rejected"
    retryable = True

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues) or "검토 거절")


class SearchUnavailableError(MarkchainError):
    """The managed search engine cannot answer right now."""

    code = "search_unavailable"
    retryable = True


class ModelError(MarkchainError):
    """The model command failed or returned output outside its contract."""

    code = "model_error"
    retryable = True
