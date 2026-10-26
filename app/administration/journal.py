"""Atomic human control results; never reuse the OS operator's trust identity."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.administration.contracts import AdminAssignmentView, OperationResult, OperationType
from app.administration.errors import AdminConflict
from app.identity.evidence import HumanAuthenticationEvidence
from app.models.identity_foundation import IdentityControlOperationTable

OPERATION_NAMESPACE = "administration.command.v1"


async def find_operation(
    session: AsyncSession, evidence: HumanAuthenticationEvidence, key: str
) -> IdentityControlOperationTable | None:
    return await session.scalar(
        select(IdentityControlOperationTable).where(
            IdentityControlOperationTable.actor_kind == "human",
            IdentityControlOperationTable.actor_ref == evidence.principal_ref,
            IdentityControlOperationTable.operation_type == OPERATION_NAMESPACE,
            IdentityControlOperationTable.idempotency_key == key,
            IdentityControlOperationTable.authentication_json["organization_ref"].as_string()
            == evidence.organization_ref,
        )
    )


def replay(row: IdentityControlOperationTable, input_digest: str | None = None) -> OperationResult:
    if input_digest is not None and row.input_digest != input_digest:
        raise AdminConflict("IDEMPOTENCY_CONFLICT")
    return OperationResult.model_validate(row.result_json).model_copy(update={"replayed": True})


def append_operation(
    session: AsyncSession,
    evidence: HumanAuthenticationEvidence,
    *,
    key: str,
    input_digest: str,
    operation_type: OperationType,
    assignment_ref: str,
    authority_version: int,
    request_id: str,
    result: AdminAssignmentView,
    before: AdminAssignmentView | None,
    reason: str | None,
) -> OperationResult:
    completed_at = datetime.now(UTC)
    response = OperationResult(
        operation_ref=str(uuid4()),
        status="succeeded",
        operation_type=operation_type,
        result=result,
        replayed=False,
        completed_at=completed_at,
    )
    authentication = evidence.audit_snapshot()
    authentication.update(
        assignment_ref=assignment_ref,
        authority_version=authority_version,
        request_id=request_id,
        reason=reason,
    )
    session.add(
        IdentityControlOperationTable(
            operation_ref=response.operation_ref,
            operation_type=OPERATION_NAMESPACE,
            idempotency_key=key,
            input_digest=input_digest,
            actor_kind="human",
            actor_ref=evidence.principal_ref,
            authentication_json=authentication,
            target_ref=result.principal_ref,
            before_json=before.model_dump(mode="json") if before else None,
            after_json=result.model_dump(mode="json"),
            result_json=response.model_dump(mode="json"),
            completed_at=completed_at,
        )
    )
    return response
