from collections.abc import Collection
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
    RoleCode.KNOWLEDGE_STEWARD: frozenset(
        {Permission.KNOWLEDGE_CANDIDATE_PROPOSE, Permission.REPOSITORY_MR_PROPOSE}
    ),
    RoleCode.PLATFORM_OPERATOR: frozenset(set(Permission)),
}


def permissions_of(roles: Collection[RoleCode]) -> frozenset[Permission]:
    """RBAC 基础能力采用显式并集；关系与属性约束在决策层处理。"""

    granted: set[Permission] = set()
    for role in roles:
        granted.update(ROLE_PERMISSIONS.get(role, frozenset()))
    return frozenset(granted)
