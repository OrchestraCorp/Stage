"""Shared synthetic builders for authorization recovery."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.authz.loader import role_permissions_digest
from app.authz.permissions import BUILTIN_POLICY_RELEASE_ID
from app.execution.storage import lock_tenant
from app.models.authorization import RelationTupleTable, RoleAssignmentTable, RoleVersionActionTable, RoleVersionTable
from app.models.identity import PrincipalTable
from app.models.identity_foundation import ExternalIdentityTable, OrganizationMembershipTable
from tests.support.authorization import ORG, PERSON, TARGET


async def _install_reader(sessions: async_sessionmaker[AsyncSession], *, another_person: bool = False) -> None:
    async with sessions.begin() as session:
        await lock_tenant(session, ORG)
        principal = "person:bob" if another_person else PERSON
        membership = "membership:bob" if another_person else "membership:alice"
        valid_from = datetime.now(UTC) - timedelta(days=1)
        if another_person:
            session.add(PrincipalTable(principal_ref=principal, principal_type="human"))
            await session.flush()
            session.add(
                OrganizationMembershipTable(
                    organization_ref=ORG,
                    membership_ref=membership,
                    principal_ref=principal,
                    principal_type="human",
                    source_ref="fixture:join",
                    valid_from=valid_from,
                )
            )
            session.add(
                ExternalIdentityTable(
                    external_identity_ref="identity:bob",
                    provider_ref="provider",
                    subject_namespace="keycloak-human-v1",
                    subject_exact="subject-2",
                    principal_ref=principal,
                    principal_type="human",
                    link_evidence_ref="fixture:link",
                )
            )
            await session.flush()
            session.add(
                RelationTupleTable(
                    organization_ref=ORG,
                    relation_ref="relation:bob:viewer",
                    membership_ref=membership,
                    subject_ref=principal,
                    subject_type="human",
                    relation="viewer_of",
                    relation_type="viewer_of",
                    resource_ref=TARGET,
                    source_kind="direct_acl",
                    source_ref="fixture:grant",
                    source_version=1,
                    valid_from=valid_from,
                )
            )
        session.add(
            RoleVersionTable(
                organization_ref=ORG,
                role_ref="role:engineering",
                role_revision=2,
                content_digest=role_permissions_digest(
                    ORG, "role:engineering", 2, BUILTIN_POLICY_RELEASE_ID, ("task.view",)
                ),
                policy_release_ref=BUILTIN_POLICY_RELEASE_ID,
            )
        )
        await session.flush()
        session.add(
            RoleVersionActionTable(
                organization_ref=ORG, role_ref="role:engineering", role_revision=2, action_ref="task.view"
            )
        )
        session.add(
            RoleAssignmentTable(
                organization_ref=ORG,
                assignment_ref="assignment:reader",
                membership_ref=membership,
                principal_ref=principal,
                principal_type="human",
                role_ref="role:engineering",
                role_revision=2,
                role_code="department-owner",
                scope_ref="scope:engineering",
                valid_from=valid_from,
                grant_source_ref="fixture:grant",
            )
        )
