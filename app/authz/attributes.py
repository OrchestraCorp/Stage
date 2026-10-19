from collections.abc import Mapping
from types import MappingProxyType

from app.authz.context import AttributeContext

CLASSIFICATION_ORDER: Mapping[str, int] = MappingProxyType(
    {"public": 10, "internal": 20, "restricted": 30, "locked": 40}
)


def evaluate_attributes(
    attributes: AttributeContext,
    *,
    require_object_version: bool = False,
    allowed_resource_states: tuple[str, ...] = (),
) -> tuple[str, ...]:
    failed: list[str] = []
    if attributes.subject_active is not True:
        failed.append("subject.active")
    resource_level = CLASSIFICATION_ORDER.get(attributes.resource_classification or "")
    ceiling_level = CLASSIFICATION_ORDER.get(attributes.classification_ceiling or "")
    if resource_level is None:
        failed.append(
            "resource.classification.missing"
            if not attributes.resource_classification
            else "resource.classification.unknown"
        )
    if ceiling_level is None:
        failed.append(
            "classification.ceiling.missing"
            if not attributes.classification_ceiling
            else "classification.ceiling.unknown"
        )
    if resource_level is not None and ceiling_level is not None and resource_level > ceiling_level:
        failed.append("resource.classification")
    if require_object_version and attributes.object_version is None:
        failed.append("object.version.missing")
    if require_object_version and attributes.expected_object_version is None:
        failed.append("object.expected_version.missing")
    for name, value in (
        ("object.version", attributes.object_version),
        ("object.expected_version", attributes.expected_object_version),
    ):
        if value is not None and (type(value) is not int or value < 1):
            failed.append(name + ".invalid")
    if (
        attributes.expected_object_version is not None
        and attributes.object_version != attributes.expected_object_version
    ):
        failed.append("object.version")
    if allowed_resource_states:
        if attributes.resource_state is None:
            failed.append("resource.state.missing")
        elif attributes.resource_state not in allowed_resource_states:
            failed.append("resource.state")
    return tuple(failed)
