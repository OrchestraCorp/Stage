"""The narrow self-identity projection grants no business or administration capability."""


def permits_self_identity(
    *,
    principal_type: str,
    principal_active: bool,
    organization_active: bool,
    membership_active: bool,
    workload_active: bool,
) -> bool:
    return (
        principal_type in {"human", "service"}
        and principal_active
        and organization_active
        and membership_active
        and (principal_type != "service" or workload_active)
    )
