__all__ = ["MarkchainError", "ConflictError", "RequestError"]


class MarkchainError(Exception):
    """An actionable failure at a Markchain boundary."""


class ConflictError(MarkchainError):
    """The current document no longer matches the saved proposal."""


class RequestError(MarkchainError):
    """The request identity or payload is invalid."""
