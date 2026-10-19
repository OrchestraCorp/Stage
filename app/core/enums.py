from enum import StrEnum


class PrincipalType(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"


class WorkLifecycle(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    COMMITTED = "committed"
    EXECUTING = "executing"
    AWAITING_ACCEPTANCE = "awaiting_acceptance"
    CLOSED = "closed"


class DecisionOutcome(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_DELEGATION = "needs_delegation"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
