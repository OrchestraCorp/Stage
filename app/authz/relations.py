from collections.abc import Iterable
from datetime import datetime

from app.authz.context import RelationTuple


def has_relation(
    relations: Iterable[RelationTuple], *, subject_ref: str, relation: str, resource_ref: str, now: datetime
) -> bool:
    return any(
        item.subject_ref == subject_ref
        and item.relation == relation
        and item.resource_ref == resource_ref
        and item.is_active_at(now)
        for item in relations
    )
