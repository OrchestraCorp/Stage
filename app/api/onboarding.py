"""Employee administration HTTP adapters; the native issuer owns password and TOTP."""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from starlette.responses import JSONResponse

from app.administration.onboarding_contracts import (
    EmployeeActivationChange,
    EmployeeActivationReissue,
    EmployeeActivationView,
    EmployeeAuditList,
    EmployeeCreateRequest,
    EmployeeDelivery,
    EmployeeOperationResult,
)
from app.api.administration import (
    _ERROR_RESPONSES,
    REFERENCE_PATTERN,
    AdministrationPageQuery,
    AdministrationQuery,
    AdministrationRoute,
    Credentials,
    IdempotencyHeader,
    IdempotencyPath,
    _call,
)
from app.providers.administration import AdministrationFacade

router = APIRouter(
    prefix="/administration", tags=["Administration"], route_class=AdministrationRoute, responses=_ERROR_RESPONSES
)

ActivationRef = Annotated[str, Path(min_length=1, max_length=128, pattern=REFERENCE_PATTERN)]


@router.post("/people", operation_id="administration_create_employee", response_model=EmployeeDelivery)
async def create_employee(
    request: Request,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    payload: EmployeeCreateRequest,
    idempotency_key: IdempotencyHeader,
) -> EmployeeDelivery | JSONResponse:
    async def update(facade: AdministrationFacade, token: str) -> EmployeeDelivery:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.create(
                evidence,
                assignment_ref=query.assignment_ref,
                request=payload,
                request_id=request.state.request_id,
                idempotency_key=idempotency_key,
            )

    return await _call(
        request,
        credentials,
        update,
        query=query,
        idempotency_key=idempotency_key,
        recovery_collection="onboarding/operations",
    )


@router.get(
    "/onboarding/activations/{activation_ref}",
    operation_id="administration_get_employee_activation",
    response_model=EmployeeActivationView,
)
async def activation_status(
    request: Request,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    activation_ref: ActivationRef,
) -> EmployeeActivationView | JSONResponse:
    async def read(facade: AdministrationFacade, token: str) -> EmployeeActivationView:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.inspect(evidence, assignment_ref=query.assignment_ref, activation_ref=activation_ref)

    return await _call(request, credentials, read, query=query)


@router.post(
    "/onboarding/activations/{activation_ref}/reissue",
    operation_id="administration_reissue_employee_activation",
    response_model=EmployeeDelivery,
)
async def reissue_activation(
    request: Request,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    activation_ref: ActivationRef,
    payload: EmployeeActivationReissue,
    idempotency_key: IdempotencyHeader,
) -> EmployeeDelivery | JSONResponse:
    async def update(facade: AdministrationFacade, token: str) -> EmployeeDelivery:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.change(
                evidence,
                assignment_ref=query.assignment_ref,
                activation_ref=activation_ref,
                request=payload,
                request_id=request.state.request_id,
                idempotency_key=idempotency_key,
                reissue=True,
            )

    return await _call(
        request,
        credentials,
        update,
        query=query,
        idempotency_key=idempotency_key,
        recovery_collection="onboarding/operations",
    )


@router.post(
    "/onboarding/activations/{activation_ref}/revoke",
    operation_id="administration_revoke_employee_activation",
    response_model=EmployeeDelivery,
)
async def revoke_activation(
    request: Request,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    activation_ref: ActivationRef,
    payload: EmployeeActivationChange,
    idempotency_key: IdempotencyHeader,
) -> EmployeeDelivery | JSONResponse:
    async def update(facade: AdministrationFacade, token: str) -> EmployeeDelivery:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.change(
                evidence,
                assignment_ref=query.assignment_ref,
                activation_ref=activation_ref,
                request=payload,
                request_id=request.state.request_id,
                idempotency_key=idempotency_key,
                reissue=False,
            )

    return await _call(
        request,
        credentials,
        update,
        query=query,
        idempotency_key=idempotency_key,
        recovery_collection="onboarding/operations",
    )


@router.get(
    "/onboarding/operations/{idempotency_key}",
    operation_id="administration_get_onboarding_operation",
    response_model=EmployeeOperationResult,
)
async def onboarding_operation(
    request: Request,
    query: Annotated[AdministrationQuery, Query()],
    credentials: Credentials,
    idempotency_key: IdempotencyPath,
) -> EmployeeOperationResult | JSONResponse:
    async def read(facade: AdministrationFacade, token: str) -> EmployeeOperationResult:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.operation(
                evidence, assignment_ref=query.assignment_ref, idempotency_key=idempotency_key
            )

    return await _call(request, credentials, read, query=query)


@router.get("/onboarding/audit", operation_id="administration_get_onboarding_audit", response_model=EmployeeAuditList)
async def onboarding_audit(
    request: Request, query: Annotated[AdministrationPageQuery, Query()], credentials: Credentials
) -> EmployeeAuditList | JSONResponse:
    async def read(facade: AdministrationFacade, token: str) -> EmployeeAuditList:
        async with facade.employee_operation(
            token, profile_ref=query.profile_ref, profile_version=query.profile_version
        ) as (service, evidence):
            return await service.audit(
                evidence, assignment_ref=query.assignment_ref, limit=query.limit, after=query.after
            )

    return await _call(request, credentials, read, query=query)
