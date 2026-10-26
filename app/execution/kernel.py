"""Execute trusted, bounded local commands atomically with audit and outbox.

Public human commands use server-issued identity evidence and the authenticated
database authorization adapter; fixture-only callers remain explicitly trusted.
Handlers must not do external I/O, commit, or change the security facts they use.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from types import MappingProxyType
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.context import AuthorizationContext
from app.authz.decision import AuthorizationDecision, AuthorizationEngine
from app.core.enums import DecisionOutcome, PrincipalType
from app.execution.contracts import (
    AuthorizationLoader,
    AuthorizationLockCoordinator,
    Clock,
    CommandEffect,
    CommandEnvelope,
    CommandIdentityValidator,
    CommandResult,
    CommandSpec,
    ExecutionContext,
    utc_clock,
)
from app.execution.encoding import copied_object, digest
from app.execution.errors import (
    AuthorizationDenied,
    CommitOutcomeUnknown,
    ExecutionNotFound,
    IdempotencyConflict,
    InvalidCommand,
    ObjectVersionConflict,
    UnknownCommand,
)
from app.execution.storage import lock_tenant, managed_uow
from app.models.execution import AuditRecordTable, CommandExecutionTable, OutboxRecordTable

logger = logging.getLogger(__name__)


class CommandKernel:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        loader: AuthorizationLoader,
        commands: Sequence[CommandSpec],
        clock: Clock = utc_clock,
        coordinator: AuthorizationLockCoordinator | None = None,
        identity_validator: CommandIdentityValidator | None = None,
    ) -> None:
        specs: dict[tuple[str, str], CommandSpec] = {}
        for spec in commands:
            key = (spec.name, spec.version)
            if key in specs:
                raise ValueError("Duplicate command registration")
            specs[key] = spec
        self._specs = MappingProxyType(specs)
        self._sessions = sessions
        self._loader = loader
        self._clock = clock
        self._authorization = AuthorizationEngine()
        self._coordinator = coordinator
        self._identity_validator = identity_validator

    def _spec(self, name: str, version: str) -> CommandSpec:
        try:
            return self._specs[name, version]
        except KeyError as exc:
            raise UnknownCommand() from exc

    async def _authorize(
        self,
        session: AsyncSession,
        context: ExecutionContext,
        *,
        action: str,
        target_ref: str,
        expected_version: int | None,
    ) -> tuple[AuthorizationDecision, AuthorizationContext]:
        now = self._clock()
        context.check_live(now)
        facts = await self._loader.load(
            session, context, action=action, target_ref=target_ref, expected_version=expected_version, now=now
        )
        # Fail closed on a loader that accidentally returns facts for a different
        # request, tenant, target or selected authority. Frozen data alone is not trust.
        assignment_ref = facts.assignment.assignment_ref if facts.assignment is not None else None
        effective_subject = facts.delegation.delegator_ref if facts.delegation else facts.subject_ref
        if (
            facts.subject_ref != context.actor_ref
            or facts.principal_type != context.principal_type
            or facts.request_id != context.request_id
            or facts.action != action
            or facts.resource_ref != target_ref
            or facts.organization_ref != context.tenant_ref
            or facts.resource_organization_ref != context.tenant_ref
            or assignment_ref != context.assignment_ref
            or facts.delegation_ref != context.delegation_ref
            or effective_subject != context.effective_subject_ref
            or (context.principal_type != PrincipalType.AGENT and context.effective_subject_ref != context.actor_ref)
        ):
            raise AuthorizationDenied()
        if expected_version is not None and facts.attributes.expected_object_version != expected_version:
            raise AuthorizationDenied()
        # Authorization time is host-owned and sampled after loading any locked facts.
        now = self._clock()
        context.check_live(now)
        decision = self._authorization.decide(replace(facts, now=now))
        if decision.outcome != DecisionOutcome.ALLOWED:
            if (
                expected_version is not None
                and facts.attributes.object_version is not None
                and expected_version != facts.attributes.object_version
            ):
                # Reveal a version conflict only after the current action's
                # remaining RBAC/ReBAC/ABAC requirements have been satisfied.
                current_version_facts = replace(
                    facts,
                    now=now,
                    attributes=replace(facts.attributes, expected_object_version=facts.attributes.object_version),
                )
                if self._authorization.decide(current_version_facts).outcome == DecisionOutcome.ALLOWED:
                    raise ObjectVersionConflict()
            raise AuthorizationDenied()
        if context.authentication is not None:
            if self._identity_validator is None:
                raise AuthorizationDenied()
            await self._identity_validator.validate_before_commit(session, context, self._clock())
            now = self._clock()
            context.check_live(now)
            decision = self._authorization.decide(replace(facts, now=now))
            if decision.outcome != DecisionOutcome.ALLOWED:
                raise AuthorizationDenied()
        return decision, facts

    @staticmethod
    def _result(row: CommandExecutionTable, *, replayed: bool) -> CommandResult:
        if row.status != "succeeded" or row.result_json is None or row.completed_at is None:
            # The current kernel only commits successful local transactions.
            # A future durable-running protocol must not silently rerun this row.
            raise CommitOutcomeUnknown(row.execution_id)
        return CommandResult(row.execution_id, copied_object(row.result_json), replayed, row.completed_at)

    async def execute(self, context: ExecutionContext, command: CommandEnvelope) -> CommandResult:
        self._check_identity_integration(context)
        spec = self._spec(command.name, command.version)
        context.check_live(self._clock())
        raw_payload = copied_object(command.payload)
        try:
            validated = spec.input_model.model_validate(raw_payload, strict=True)
            normalized = copied_object(validated.model_dump(mode="json"))
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidCommand() from exc
        command = replace(command, payload=normalized)
        payload_digest = digest(
            {
                "encoding": "stage-command-json-v1",
                "target": command.target_ref,
                "expected_version": command.expected_version,
                "payload": normalized,
            }
        )
        context_digest = digest(
            {
                "principal_type": context.principal_type.value,
                "effective_subject": context.effective_subject_ref,
                "assignment": context.assignment_ref,
                "delegation": context.delegation_ref,
            }
        )
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            if self._coordinator is not None:
                await self._coordinator.acquire_locks(session, context)
                uow.assert_owned()
            await lock_tenant(session, context.tenant_ref)
            row = await session.scalar(
                select(CommandExecutionTable).where(
                    CommandExecutionTable.tenant_ref == context.tenant_ref,
                    CommandExecutionTable.actor_ref == context.actor_ref,
                    CommandExecutionTable.command_name == command.name,
                    CommandExecutionTable.command_version == command.version,
                    CommandExecutionTable.idempotency_key == command.idempotency_key,
                )
            )
            if row is not None:
                await self._authorize(
                    session, context, action=spec.read_action, target_ref=row.target_ref, expected_version=None
                )
                uow.assert_owned()
                if row.payload_digest != payload_digest or row.context_digest != context_digest:
                    raise IdempotencyConflict()
                return self._result(row, replayed=True)

            decision, guarded_facts = await self._authorize(
                session,
                context,
                action=spec.execute_action,
                target_ref=command.target_ref,
                expected_version=command.expected_version,
            )
            uow.assert_owned()
            row = CommandExecutionTable(
                execution_id=str(uuid4()),
                tenant_ref=context.tenant_ref,
                actor_ref=context.actor_ref,
                effective_subject_ref=context.effective_subject_ref,
                command_name=command.name,
                command_version=command.version,
                idempotency_key=command.idempotency_key,
                payload_digest=payload_digest,
                context_digest=context_digest,
                target_ref=command.target_ref,
                expected_version=command.expected_version,
                request_id=context.request_id,
                assignment_ref=context.assignment_ref,
                delegation_ref=context.delegation_ref,
                status="running",
                created_at=self._clock(),
            )
            session.add(row)
            await uow.flush()
            effect = await spec.handler(uow, context, command, validated)
            uow.assert_owned()
            if not isinstance(effect, CommandEffect):
                raise InvalidCommand()
            try:
                result = spec.result_model.model_validate(copied_object(effect.result), strict=True)
                result_data = copied_object(result.model_dump(mode="json"))
            except (ValidationError, ValueError, TypeError) as exc:
                raise InvalidCommand() from exc
            row.result_json = result_data
            row.status = "succeeded"
            row.completed_at = self._clock()
            session.add(
                AuditRecordTable(
                    audit_id=str(uuid4()),
                    tenant_ref=context.tenant_ref,
                    execution_id=row.execution_id,
                    actor_ref=context.actor_ref,
                    effective_subject_ref=context.effective_subject_ref,
                    action=spec.execute_action,
                    target_ref=command.target_ref,
                    decision_id=decision.decision_id,
                    policy_release_id=decision.policy_release_id,
                    request_id=context.request_id,
                    before_json=copied_object(effect.before) if effect.before is not None else None,
                    after_json=copied_object(effect.after) if effect.after is not None else None,
                    authorization_json=self._authorization_evidence(guarded_facts, decision, context),
                    created_at=row.completed_at,
                )
            )
            if len(effect.messages) > 100:
                raise InvalidCommand()
            for intent in effect.messages:
                session.add(
                    OutboxRecordTable(
                        message_id=str(uuid4()),
                        tenant_ref=context.tenant_ref,
                        execution_id=row.execution_id,
                        topic=intent.topic,
                        destination=intent.destination,
                        aggregate_ref=intent.aggregate_ref,
                        payload_json=copied_object(intent.payload),
                        read_action=intent.read_action,
                        dedupe_key=intent.dedupe_key,
                        status="pending",
                        available_at=row.completed_at,
                        attempt_count=0,
                        created_at=row.completed_at,
                    )
                )
            # Constraint failures happen before the commit boundary. They
            # roll back the domain write, execution, audit and messages together.
            await uow.flush()
            if context.authentication is not None:
                assert self._identity_validator is not None
                await self._identity_validator.validate_before_commit(session, context, self._clock())
            now = self._clock()
            context.check_live(now)
            # Guarded authorization inputs remain stable under the writer
            # protocol, but time advances. Recheck their temporal validity
            # against the pre-write resource state, not the mutated domain row.
            if self._authorization.decide(replace(guarded_facts, now=now)).outcome != DecisionOutcome.ALLOWED:
                raise AuthorizationDenied()
            completed = self._result(row, replayed=False)
            try:
                await uow._commit_by_owner()
            except BaseException as exc:
                logger.warning("command commit outcome unknown execution_id=%s", completed.execution_id)
                raise CommitOutcomeUnknown(completed.execution_id) from exc
            return completed

    async def get_result(self, context: ExecutionContext, execution_id: str) -> CommandResult:
        """Current authorized read for the same tenant and authenticated actor.

        Acquiring the guard also waits for an earlier transaction to finish.
        A missing row here is not a general cross-system proof of non-execution.
        """
        self._check_identity_integration(context)
        context.check_live(self._clock())
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            if self._coordinator is not None:
                await self._coordinator.acquire_locks(session, context)
                uow.assert_owned()
            await lock_tenant(session, context.tenant_ref)
            row = await session.scalar(
                select(CommandExecutionTable).where(
                    CommandExecutionTable.tenant_ref == context.tenant_ref,
                    CommandExecutionTable.actor_ref == context.actor_ref,
                    CommandExecutionTable.execution_id == execution_id,
                )
            )
            if row is None:
                raise ExecutionNotFound()
            spec = self._spec(row.command_name, row.command_version)
            await self._authorize(
                session, context, action=spec.read_action, target_ref=row.target_ref, expected_version=None
            )
            uow.assert_owned()
            return self._result(row, replayed=True)

    async def read[T](
        self,
        context: ExecutionContext,
        *,
        action: str,
        target_ref: str,
        reader: Callable[[AsyncSession], Awaitable[T]],
    ) -> T:
        """Load a value under current authorization and recheck before disclosure.

        The trusted reader must return a materialized value, perform only local
        database reads, and leave transaction ownership to this method. The
        transaction always rolls back; writes belong in registered commands.
        Authentication/authorization locks remain held through the final check.
        """
        self._check_identity_integration(context)
        context.check_live(self._clock())
        async with managed_uow(self._sessions) as uow:
            if self._coordinator is not None:
                await self._coordinator.acquire_locks(uow.session, context)
                uow.assert_owned()
            await lock_tenant(uow.session, context.tenant_ref)
            await self._authorize(uow.session, context, action=action, target_ref=target_ref, expected_version=None)
            uow.assert_owned()
            result = await reader(uow.session)
            uow.assert_owned()
            # Data loading can cross authentication or grant expiration. A read
            # has the same current-identity boundary as a command's final commit.
            await self._authorize(uow.session, context, action=action, target_ref=target_ref, expected_version=None)
            uow.assert_owned()
            return result

    async def get_result_by_key(
        self,
        context: ExecutionContext,
        *,
        command_name: str,
        command_version: str,
        idempotency_key: str,
        target_ref: str,
    ) -> CommandResult:
        """Recover an uncertain HTTP result using the caller's original operation key."""
        self._check_identity_integration(context)
        context.check_live(self._clock())
        spec = self._spec(command_name, command_version)
        async with managed_uow(self._sessions) as uow:
            session = uow.session
            if self._coordinator is not None:
                await self._coordinator.acquire_locks(session, context)
                uow.assert_owned()
            await lock_tenant(session, context.tenant_ref)
            row = await session.scalar(
                select(CommandExecutionTable).where(
                    CommandExecutionTable.tenant_ref == context.tenant_ref,
                    CommandExecutionTable.actor_ref == context.actor_ref,
                    CommandExecutionTable.command_name == command_name,
                    CommandExecutionTable.command_version == command_version,
                    CommandExecutionTable.idempotency_key == idempotency_key,
                    CommandExecutionTable.target_ref == target_ref,
                )
            )
            if row is None:
                raise ExecutionNotFound()
            await self._authorize(
                session, context, action=spec.read_action, target_ref=target_ref, expected_version=None
            )
            uow.assert_owned()
            return self._result(row, replayed=True)

    def _check_identity_integration(self, context: ExecutionContext) -> None:
        if context.authentication is not None and (self._coordinator is None or self._identity_validator is None):
            raise AuthorizationDenied()

    @staticmethod
    def _authorization_evidence(
        facts: AuthorizationContext,
        decision: AuthorizationDecision,
        context: ExecutionContext,
    ) -> dict[str, object] | None:
        assignment = facts.assignment
        if assignment is None or assignment.permission_actions is None:
            return None
        evidence: dict[str, object] = {
            "schema": "stage-authorization-evidence-v1",
            "organization_ref": facts.organization_ref,
            "principal_ref": facts.subject_ref,
            "membership_ref": assignment.membership_ref,
            "assignment_ref": assignment.assignment_ref,
            "role_ref": assignment.role_ref,
            "role_revision": assignment.role_revision,
            "role_digest": assignment.role_version_digest,
            "policy_release_ref": facts.policy_release_id,
            "action": facts.action,
            "resource_ref": facts.resource_ref,
            "resource_scopes": list(facts.resource_scope_refs),
            "matched_relations": list(decision.matched_relations),
            "fact_versions": [list(version) for version in facts.fact_versions],
            "evaluated_at": decision.evaluated_at.isoformat() if decision.evaluated_at else None,
        }
        if context.authentication is not None:
            evidence["authentication"] = context.authentication.audit_snapshot()
        return evidence
