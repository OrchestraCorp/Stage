
from app.authz.context import AttributeContext

CLASSIFICATION_ORDER = {"public": 10, "internal": 20, "restricted": 30, "locked": 40}


def evaluate_attributes(attributes: AttributeContext) -> tuple[str, ...]:
    failed: list[str] = []
    if not attributes.subject_active:
        failed.append("subject.active")
    if not attributes.delegation_active:
        failed.append("delegation.active")
    if (
        attributes.expected_object_version is not None
        and attributes.object_version != attributes.expected_object_version
    ):
        failed.append("object.version")
    if attributes.resource_state is not None and attributes.allowed_resource_states:
        if attributes.resource_state not in attributes.allowed_resource_states:
            failed.append("resource.state")
    resource_level = CLASSIFICATION_ORDER.get(attributes.resource_classification)
    ceiling_level = CLASSIFICATION_ORDER.get(attributes.classification_ceiling)
    if resource_level is None or ceiling_level is None or resource_level > ceiling_level:
        failed.append("resource.classification")
    return tuple(failed)
