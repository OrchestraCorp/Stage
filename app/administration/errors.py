"""Deliberately small public error vocabulary for human administration."""


class AdminDenied(Exception):
    """The current caller cannot access this management operation or target."""


class AdminConflict(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AdminNotFound(Exception):
    """No currently authorized result or target is available."""


class AdminCommitUnknown(Exception):
    """Commit was attempted; the caller must recover using the original key."""

    def __init__(self, assignment_ref: str) -> None:
        self.assignment_ref = assignment_ref
        super().__init__("COMMIT_OUTCOME_UNKNOWN")
