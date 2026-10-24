"""Stable internal outcomes. Exception messages never include payloads or SQL."""


class ExecutionError(Exception):
    code = "EXECUTION_ERROR"

    def __init__(self) -> None:
        super().__init__(self.code)


class InvalidCommand(ExecutionError):
    code = "COMMAND_INVALID"


class UnknownCommand(ExecutionError):
    code = "COMMAND_UNKNOWN"


class AuthorizationDenied(ExecutionError):
    code = "AUTHORIZATION_DENIED"


class ContextExpired(ExecutionError):
    code = "EXECUTION_CONTEXT_EXPIRED"


class TenantNotProvisioned(ExecutionError):
    code = "TENANT_NOT_PROVISIONED"


class IdempotencyConflict(ExecutionError):
    code = "IDEMPOTENCY_CONFLICT"


class ExecutionNotFound(ExecutionError):
    code = "EXECUTION_NOT_FOUND"


class TransactionOwnershipError(ExecutionError):
    code = "TRANSACTION_OWNERSHIP_VIOLATION"


class CommitOutcomeUnknown(ExecutionError):
    code = "COMMIT_OUTCOME_UNKNOWN"

    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        super().__init__()


class ExecutionCleanupFailed(ExecutionError):
    code = "EXECUTION_CLEANUP_FAILED"
