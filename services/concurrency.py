DATA_CONFLICT_MESSAGE = "Данные изменены другим рабочим местом. Обновите карточку."


class DataConflictError(RuntimeError):
    """Raised when an optimistic-lock revision check detects stale data."""


class OrderMarkConflictError(DataConflictError):
    """A rejected mark includes the revision needed to recover the display."""

    def __init__(self, message, *, reason="", current_version=None):
        super().__init__(message)
        self.reason = reason
        self.current_version = current_version


def normalize_expected_revision(expected_revision):
    if expected_revision is None:
        return None
    return int(expected_revision)


def assert_revision_matches(current_revision, expected_revision):
    expected = normalize_expected_revision(expected_revision)
    if expected is None:
        return
    current = int(current_revision or 0)
    if current != expected:
        raise DataConflictError(DATA_CONFLICT_MESSAGE)
