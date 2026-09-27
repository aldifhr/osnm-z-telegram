"""Application-level mint orchestration failures."""


class MintError(RuntimeError):
    """The selected mint could not be prepared, submitted, or confirmed."""
