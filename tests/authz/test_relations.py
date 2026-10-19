from datetime import UTC, datetime, timedelta

from app.authz.context import RelationTuple
from app.authz.relations import has_relation


def test_relation_is_time_bounded() -> None:
    now = datetime.now(UTC)
    relations = (
        RelationTuple(
            subject_ref="member:alice",
            relation="assignee_of",
            resource_ref="work_item:1",
            expires_at=now + timedelta(minutes=5),
        ),
    )
    assert has_relation(
        relations, subject_ref="member:alice", relation="assignee_of", resource_ref="work_item:1", now=now
    )
    assert not has_relation(
        relations,
        subject_ref="member:alice",
        relation="assignee_of",
        resource_ref="work_item:1",
        now=now + timedelta(minutes=6),
    )
