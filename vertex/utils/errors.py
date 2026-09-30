"""Error taxonomy: retryable vs fatal, so job loops never abort on a single failure."""


class VertexError(Exception):
    """Base class."""


class RetryableError(VertexError):
    """Transient failure (timeout, rate limit, network). Safe to retry with backoff."""


class FatalError(VertexError):
    """Permanent failure for this input (validation, bad args). Do not retry."""


class GateRefused(VertexError):
    """An enrollment or spend gate refused the action. Carries the reasons."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class BudgetExceeded(VertexError):
    """A credit or cost budget would be exceeded."""
