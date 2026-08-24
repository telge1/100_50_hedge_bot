"""Internal invariant failures for P4A validation."""


class ValidationInvariantError(Exception):
    """Raised when P4A encounters a state excluded by closed catalog integrity."""
