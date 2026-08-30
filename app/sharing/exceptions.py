"""Stable exceptions raised by the shared-guide bounded context."""


class SharedGuideNotFoundError(LookupError):
    """The requested shared guide does not exist or is not visible."""


class SharedGuideConflictError(RuntimeError):
    """The requested state transition conflicts with the current guide state."""


class SharedGuideForbiddenError(PermissionError):
    """The caller is not allowed to perform the shared-guide operation."""


class SharedGuideUnavailableError(RuntimeError):
    """A required indexing dependency is temporarily unavailable."""


class InvalidShareCursorError(ValueError):
    """A shared-guide listing cursor cannot be decoded or validated."""


class StaleIndexVersionError(RuntimeError):
    """An index operation targets an obsolete shared-guide version."""
