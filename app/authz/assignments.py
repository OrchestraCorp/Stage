"""Bounded discovery of the authenticated person's current assignment choices."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz.loader import load_role_version, load_scope_chain
from app.identity.evidence import HumanAuthenticationEvidence
from app.models.authorization import RoleAssignmentTable


class AssignmentView(BaseModel):
    """A selection hint, never a permission decision or reusable grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_ref: str
    role_ref: str
    role_revision: int = Field(ge=1)
    role_code: str
    role_display_name: str
    scope_ref: str
    valid_from: datetime
    expires_at: datetime | None
    version: int = Field(ge=1)


class AssignmentList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[AssignmentView, ...]
    next_after: str | None = Field(
        description="Continue after this assignment reference; a filtered page may be empty and still have a cursor."
    )

    def current_at(self, now: datetime) -> "AssignmentList":
        return self.model_copy(
            update={
                "items": tuple(
                    item
                    for item in self.items
                    if item.valid_from <= now and (item.expires_at is None or now < item.expires_at)
                )
            }
        )


async def load_self_assignments(
    session: AsyncSession,
    evidence: HumanAuthenticationEvidence,
    *,
    now: datetime,
    limit: int = 20,
    after: str | None = None,
) -> AssignmentList:
    """The caller holds identity locks and the organization guard after validation.

    Read at most limit + 1 candidate rows, inspect only the first limit, and
    never scan ahead to replace invalid roles/scopes. A cursor is an ordering
    position only: no row lookup or existence disclosure is performed for it.
    """
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("assignment page limit must be between 1 and 50")
    query = select(RoleAssignmentTable).where(
        RoleAssignmentTable.organization_ref == evidence.organization_ref,
        RoleAssignmentTable.principal_ref == evidence.principal_ref,
        RoleAssignmentTable.principal_type == "human",
        RoleAssignmentTable.membership_ref == evidence.membership_ref,
        RoleAssignmentTable.assignment_ref.is_not(None),
        RoleAssignmentTable.role_ref.is_not(None),
        RoleAssignmentTable.role_revision.is_not(None),
        RoleAssignmentTable.status == "active",
        RoleAssignmentTable.revoked_at.is_(None),
        RoleAssignmentTable.valid_from <= now,
        or_(RoleAssignmentTable.expires_at.is_(None), RoleAssignmentTable.expires_at > now),
    )
    if after is not None:
        query = query.where(RoleAssignmentTable.assignment_ref > after)
    candidates = tuple(
        await session.scalars(
            query.order_by(RoleAssignmentTable.assignment_ref)
            .limit(limit + 1)
            .execution_options(populate_existing=True)
        )
    )
    selected = candidates[:limit]
    items: list[AssignmentView] = []
    for assignment in selected:
        role = await load_role_version(session, evidence.organization_ref, assignment)
        if role is None or not await load_scope_chain(session, evidence.organization_ref, assignment.scope_ref):
            continue
        # The SQL filters already exclude unmapped legacy rows. Keep explicit
        # checks so a broken internal adapter never fabricates an identity.
        if assignment.assignment_ref is None or assignment.valid_from is None:
            continue
        items.append(
            AssignmentView(
                assignment_ref=assignment.assignment_ref,
                role_ref=role.role.role_ref,
                role_revision=role.revision.role_revision,
                role_code=role.role.role_code,
                role_display_name=role.role.display_name,
                scope_ref=assignment.scope_ref,
                valid_from=assignment.valid_from,
                expires_at=assignment.expires_at,
                version=assignment.version,
            )
        )
    return AssignmentList(
        items=tuple(items),
        next_after=selected[-1].assignment_ref if len(candidates) > limit else None,
    )
