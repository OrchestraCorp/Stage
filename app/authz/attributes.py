from app.authz.context import AttributeContext

CLASSIFICATION_ORDER = {"public": 10, "internal": 20, "restricted": 30, "locked": 40}


def evaluate_attributes(attributes: AttributeContext, *, require_object_version: bool = False) -> tuple[str, ...]:
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
    if (
        attributes.expected_object_version is not None
        and attributes.object_version != attributes.expected_object_version
    ):
        failed.append("object.version")
    if attributes.resource_state is not None and attributes.allowed_resource_states:
        if attributes.resource_state not in attributes.allowed_resource_states:
            failed.append("resource.state")
    return tuple(failed)
