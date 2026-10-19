from datetime import UTC, datetime, timedelta

from app.authz.context import RelationTuple
from app.authz.relations import has_relation, relation_for_group


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


def test_matching_relation_is_deterministic_across_evidence_order() -> None:
    now = datetime.now(UTC)
    relations = (
        RelationTuple("member:alice", "reviewer_of", "work_item:1"),
        RelationTuple("member:alice", "owner_of", "work_item:1"),
    )
    for evidence in (relations, tuple(reversed(relations))):
        assert (
            relation_for_group(
                evidence,
                subjects=("member:alice",),
                relation_names=frozenset({"reviewer_of", "owner_of"}),
                resource_ref="work_item:1",
                now=now,
            )
            == "owner_of"
        )


def test_relation_evidence_can_be_a_one_shot_iterable() -> None:
    relations = iter((RelationTuple("member:alice", "reviewer_of", "work_item:1"),))
    assert (
        relation_for_group(
            relations,
            subjects=("member:alice",),
            relation_names=frozenset({"owner_of", "reviewer_of"}),
            resource_ref="work_item:1",
            now=datetime.now(UTC),
        )
        == "reviewer_of"
    )
