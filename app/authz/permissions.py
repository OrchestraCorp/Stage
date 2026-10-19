from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    TASK_VIEW = "task.view"
    TASK_COMMIT_CREATE = "task.commit.create"
    TASK_DELIVERY_SUBMIT = "task.delivery.submit"
    TASK_ACCEPTANCE_REVIEW = "task.acceptance.review"
    TASK_ASSIGN = "task.assign"
    KNOWLEDGE_CANDIDATE_PROPOSE = "knowledge.candidate.propose"
    REPOSITORY_MR_PROPOSE = "repository.mr.propose"


class RoleCode(StrEnum):
    ENGINEERING_MEMBER = "engineering-member"
    DEPARTMENT_OWNER = "department-owner"
    KNOWLEDGE_STEWARD = "knowledge-steward"
    PLATFORM_OPERATOR = "platform-operator"


ROLE_PERMISSIONS: dict[RoleCode, frozenset[Permission]] = {
    RoleCode.ENGINEERING_MEMBER: frozenset(
        {Permission.TASK_VIEW, Permission.TASK_COMMIT_CREATE, Permission.TASK_DELIVERY_SUBMIT}
    ),
    RoleCode.DEPARTMENT_OWNER: frozenset(
        {Permission.TASK_VIEW, Permission.TASK_ASSIGN, Permission.TASK_ACCEPTANCE_REVIEW}
    ),
    RoleCode.KNOWLEDGE_STEWARD: frozenset({Permission.KNOWLEDGE_CANDIDATE_PROPOSE, Permission.REPOSITORY_MR_PROPOSE}),
    # Explicitly empty: platform operations must use separately registered
    # technical capabilities and never become a business superuser.
    RoleCode.PLATFORM_OPERATOR: frozenset(),
}


@dataclass(frozen=True)
class ActionPolicy:
    permission: Permission
    relation_any_of: tuple[frozenset[str], ...]
    require_object_version: bool = False
    allowed_resource_states: tuple[str, ...] = ()


ACTION_POLICIES: dict[Permission, ActionPolicy] = {
    Permission.TASK_VIEW: ActionPolicy(
        Permission.TASK_VIEW, (frozenset({"viewer_of", "assignee_of", "owner_of", "reviewer_of"}),)
    ),
    Permission.TASK_COMMIT_CREATE: ActionPolicy(
        Permission.TASK_COMMIT_CREATE, (frozenset({"assignee_of"}),), require_object_version=True
    ),
    Permission.TASK_DELIVERY_SUBMIT: ActionPolicy(
        Permission.TASK_DELIVERY_SUBMIT, (frozenset({"assignee_of"}),), require_object_version=True
    ),
    Permission.TASK_ACCEPTANCE_REVIEW: ActionPolicy(
        Permission.TASK_ACCEPTANCE_REVIEW, (frozenset({"reviewer_of", "owner_of"}),), require_object_version=True
    ),
    Permission.TASK_ASSIGN: ActionPolicy(
        Permission.TASK_ASSIGN, (frozenset({"owner_of"}),), require_object_version=True
    ),
    Permission.KNOWLEDGE_CANDIDATE_PROPOSE: ActionPolicy(
        Permission.KNOWLEDGE_CANDIDATE_PROPOSE,
        (frozenset({"contributor_of", "steward_of"}),),
        require_object_version=True,
    ),
    Permission.REPOSITORY_MR_PROPOSE: ActionPolicy(
        Permission.REPOSITORY_MR_PROPOSE,
        (frozenset({"contributor_of", "maintainer_of"}),),
        require_object_version=True,
    ),
}


def permissions_of(roles: Collection[RoleCode]) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for role in roles:
        granted.update(ROLE_PERMISSIONS.get(role, frozenset()))
    return frozenset(granted)
