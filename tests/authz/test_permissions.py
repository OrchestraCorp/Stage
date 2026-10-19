from app.authz.permissions import Permission, RoleCode, permissions_of


def test_role_permissions_use_explicit_union() -> None:
    member = permissions_of((RoleCode.ENGINEERING_MEMBER,))
    owner = permissions_of((RoleCode.DEPARTMENT_OWNER,))
    combined = permissions_of((RoleCode.ENGINEERING_MEMBER, RoleCode.DEPARTMENT_OWNER))

    assert Permission.TASK_DELIVERY_SUBMIT in member
    assert Permission.TASK_ACCEPTANCE_REVIEW in owner
    assert member | owner == combined
