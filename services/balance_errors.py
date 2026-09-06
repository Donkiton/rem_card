class IncompleteBalanceError(RuntimeError):
    """A required balance input could not be read; zero is not a substitute."""
