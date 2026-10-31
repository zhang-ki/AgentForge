"""Expected, user-facing AgentForge errors."""


class AgentForgeError(Exception):
    """Base class for errors that can be reported without a traceback."""


class ScanError(AgentForgeError):
    """The requested repository cannot be scanned safely."""


class StoreError(AgentForgeError):
    """The local index could not be opened or updated."""


class IndexConflictError(StoreError):
    """The fixed local index is already bound to a different repository."""


class ProviderError(AgentForgeError):
    """A provider failed or returned an invalid response."""


class CitationValidationError(ProviderError):
    """A model citation violated the request-local evidence protocol."""

    def __init__(self, message: str, *, code: str, diagnostic: dict):
        super().__init__(message)
        self.code = code
        self.diagnostic = diagnostic

