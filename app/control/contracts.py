"""Domain-neutral contracts for explicitly authorized local administration."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Reference = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Environment = Literal["dev", "test"]
BOOTSTRAP_TYPE = "identity-foundation"


class AdministrationError(Exception):
    """Stable public error code; underlying database or configuration data stays private."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OperationUnknown(Exception):
    """A database failure does not prove whether the operation committed."""

    def __init__(self, operation_type: str, idempotency_key: str) -> None:
        self.operation_type = operation_type
        self.idempotency_key = idempotency_key
        super().__init__("result_unknown")


class StrictPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdministrationResult(StrictPlan):
    operation_ref: str
    result: dict[str, Any]
