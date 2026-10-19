__all__ = [
    "ConflictError",
    "guidance",
    "MarkdownNotesAgentError",
    "ModelError",
    "RequestError",
    "ReviewRejectedError",
    "SearchUnavailableError",
]


class MarkdownNotesAgentError(Exception):
    """An actionable failure at a Markdown Notes Agent boundary."""

    code = "error"
    retryable = False

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConflictError(MarkdownNotesAgentError):
    """The current document no longer matches the saved proposal."""

    code = "source_changed"
    retryable = True


class RequestError(MarkdownNotesAgentError):
    """The request identity or payload is invalid."""

    code = "invalid_request"


class ReviewRejectedError(MarkdownNotesAgentError):
    """The content review rejected a generated proposal."""

    code = "review_rejected"
    retryable = True

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues) or "The review did not approve the change.")


class SearchUnavailableError(MarkdownNotesAgentError):
    """The managed search engine cannot answer right now."""

    code = "search_unavailable"
    retryable = True


class ModelError(MarkdownNotesAgentError):
    """The model command failed or returned output outside its contract."""

    code = "model_error"
    retryable = True


_TELL = "Tell the user: "
_THEN = "\nThen: "


class ModelOutputError(ModelError):
    """The model answered, but not in the required format."""


def guidance(tell: str, then: str = "") -> str:
    """A message for an AI client: what to tell the user, then what to do.

    The client relays the "Tell the user" part as it is, translated, so the
    user hears the same words from every client.
    """
    return _TELL + tell + (_THEN + then if then else "")


def for_terminal(message: str) -> str:
    """The part of a message meant for the user, without the AI instructions."""
    if not message.startswith(_TELL):
        return message
    return message[len(_TELL) :].split(_THEN, 1)[0]
