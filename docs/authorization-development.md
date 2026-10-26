---
title: 人员授权与工作项开发操作
status: development-only
review_date: 2026-10-25
policy_effect: none
---

# 人员授权与工作项开发操作

本工具为已经完成身份初始化的 **dev/test** 数据库安装有限人员授权事实。操作者是本机 OS 安装身份，工具不验证人类登录 token，也不是 HTTP 人员管理或正式企业治理批准接口。P1b.3 已另行接通真实人员认证到最小元数据命令，两条操作路径的身份与职责保持独立。

运行前由受信任终端显式设置 `STAGE_ENV` 和 `STAGE_DATABASE_URL`，完成 `alembic upgrade head`。本机配置加载方式见[开发设备运行说明](../deployment/dev/README.md)；不要把数据库 URL、token、凭据文件内容写进计划或日志。计划中的组织根、主体、成员、版本和 WorkItem 必须来自当前数据库，不能把示例引用当成实际记录。

## 1. 安装一个角色版本与授权

`scripts/authorization_admin.py install` 每次追加一个固定角色版本与明确任职，按需新增 scope、登记已有 WorkItem 和直接 ACL。允许动作 `task.view/task.assign/task.metadata.update` 与关系 `viewer_of/owner_of`。工具不创建主体、成员或 WorkItem，不注册任意 action 或伪造任务分派/验收事实。新增 metadata action 不会改写已发布角色版本；需要编辑权限必须显式发布新 revision 并指定任职。

结构示例（虚构引用；执行前替换为已核验的事实）：

```json
{
  "environment": "dev",
  "organization_ref": "example-organization",
  "expected_organization_version": 1,
  "role_ref": "example-reader",
  "role_code": "example-reader",
  "display_name": "Example reader",
  "role_revision": 1,
  "actions": ["task.view"],
  "scopes": [
    {"scope_ref": "example-project", "scope_kind": "project"}
  ],
  "assignments": [
    {
      "assignment_ref": "example-assignment",
      "principal_ref": "example-human",
      "membership_ref": "example-membership",
      "expected_membership_version": 1,
      "scope_ref": "example-project"
    }
  ],
  "resources": [
    {
      "resource_ref": "work_item:1",
      "scope_ref": "example-project",
      "expected_work_version": 1
    }
  ],
  "acls": [
    {
      "relation_ref": "example-viewer",
      "principal_ref": "example-human",
      "membership_ref": "example-membership",
      "expected_membership_version": 1,
      "relation": "viewer_of",
      "resource_ref": "work_item:1"
    }
  ]
}
```

WorkItem 必须已存在，其旧 `scope_ref` 与登记 scope 完全一致。已有 scope/resource 不重复列入新增数组；任职和 ACL 可以引用已存在且有效的 scope/resource。每次安装至少创建一项明确任职，不提供原位编辑任职或追加旧角色版本 action 的接口。

```bash
uv run python scripts/authorization_admin.py --environment dev install \
  --plan /absolute/path/authorization-plan.json \
  --idempotency-key example-authorization-install-v1
```

为同一角色发布新 revision 时必须提供 `expected_role_definition_version`，角色 code/name 保持一致，新 revision 与 assignment 使用明确引用。旧任职仍指向原 revision；新版本不会自动扩大它们的权限。成员已经结束或到期时拒绝安装，重新加入的成员必须获得新任职与新 ACL。

角色、任职、ACL、scope/resource、组织授权 epoch 和操作 journal 在同一个 PostgreSQL 事务提交。重放相同操作者、操作类型、幂等键和输入返回原结果；同键不同输入冲突。已撤销的对象不会因重放安装恢复。角色版本不可变由注册器的追加规则和 loader 摘要校验保证，当前没有数据库 trigger 防止拥有直接写权限的宿主绕过规则。

## 2. 撤销与结果恢复

支持五种 `--kind`：`assignment`、`relation`、`role_version`、`scope`、`resource`。必须提供当前 `--expected-version`；角色版本另传 `--role-revision`。

```bash
uv run python scripts/authorization_admin.py --environment dev revoke \
  --organization-ref example-organization \
  --kind assignment --target-ref example-assignment --expected-version 1 \
  --idempotency-key example-assignment-revoke-v1

uv run python scripts/authorization_admin.py --environment dev result \
  --operation-type revoke.authorization.assignment \
  --idempotency-key example-assignment-revoke-v1
```

操作结果不明确时，保留原幂等键与输入，先通过原 OS 操作者查询 `result`；不要换键重复写入。安装查询类型为 `install.authorization`，五种撤销查询类型为 `revoke.authorization.<kind>`。撤销状态、行版本、组织 epoch 和 journal 原子提交；撤销角色版本会阻断引用它的任职，撤销祖先 scope 会阻断该 scope 链下的受控动作。历史审计和命令结果不删除。

## 3. 人员命令装配与 HTTP 边界

公开人员操作使用同一个 `AuthenticatedAuthorizationLoader` 作为 loader、coordinator 和提交前 identity validator；它包装 `DatabaseAuthorizationLoader`，以 trust/principal 共享锁→组织 guard 的顺序重验真实证据、固定成员经历、单任职、角色版本、scope、资源和 direct ACL。旧任职/关系未映射字段不进入授权路径，原内部 fixture loader 不公开。

`ExecutionContext` 由服务端依据真实人员认证证据构造；`/identity/me` 响应不是权限票据，请求 JSON 不能指定主体、组织或能力。最小编辑要求显式 `task.metadata.update`、owner_of、draft/open 与预期版本，分类上限固定 internal；技术服务 token 不取得人员权限。完整任务分派/验收按后续业务切片实施，独立 Agent/Delegation 与通用 ServiceGrant 继续后置，见[一期协作边界](people-company-collaboration.md)。

业务 handler 不得更改本次授权依赖的安全事实；这是可信代码合同，不是 Python/数据库沙箱。授权变更通过专用协调服务实施，不能因为普通 handler 能取得事务 Session 就直接修改任职、ACL 或角色版本。

已登记的工作项通过 `GET /api/v1/work-items/{work_item_id}` 读取。读取、编辑和结果查询统一要求当前人员 Bearer 及 query `profile_ref/profile_version/assignment_ref`，不存在客户端组织选择。最小编辑请求：

```http
PUT /api/v1/work-items/1/details?profile_ref=example-human&profile_version=1&assignment_ref=example-assignment
Authorization: Bearer <current_human_access_token>
Idempotency-Key: example-edit-once
Content-Type: application/json

{"title":"Updated title","description":null,"expected_version":1}
```

上述引用是格式示例，不会自动创建对应对象或权限。`description` 必须明确提供文本或 `null`；PUT 只改标题、说明和版本，不修改状态、scope、分类，也不表示批准或验收。编辑角色版本需显式包含 `task.metadata.update`；读取和结果恢复需 `task.view`，编辑者还需目标的 `owner_of`。

提交未知或网络中断时保留原输入与 key，用 `GET /api/v1/work-items/1/operations/example-edit-once` 查询，携带当前认证和有读权限的任职。GET 结果恢复可以使用同一人员当前获权的另一个任职；PUT 重放改变原任职则幂等冲突。不要换 key 重复写入。字段范围、错误和审计见[P1b.3 记录](reviews/p1b3-human-work-2026-10-25.md)。

## 4. 独立 PostgreSQL 迁移验收

`scripts/check_authorization_migration.py` 只用于**明确可丢弃且为空的 PostgreSQL 测试库**。它要求 `TEST_DATABASE_URL` 和 `STAGE_ALLOW_TEST_DATABASE_RESET=1`，拒绝非 PostgreSQL 或非空数据库。不要指向 Stage 开发库或任何需保留的企业数据。

在受信任终端为上述变量设置独立测试库配置后执行：

```bash
uv run python scripts/check_authorization_migration.py
```

脚本将数据库迁移到前一版，插入默认序列生成的旧授权行，再升级、降级、重新升级，核验 ID 连续为 1、2、3、4，原行保留并通过 head schema check。完成后留下合成数据供检查，重复执行需要另一个明确为空的测试库。脚本不负责清空现有数据库，也不把可验证降级解释为真实环境的推荐回滚方案。

授权表迁移和内部 loader 的历史验收见[P1b.2](reviews/p1b2-authorization-2026-10-25.md)，真实人员命令和 HTTP 的本轮验收见[P1b.3](reviews/p1b3-human-work-2026-10-25.md)。P1b.3 当时未新增迁移；后续 P1b.4 增加组织人员认证策略，该轮为 26 张应用表；本轮原生认证新增八张，当前为 37 张、head `c6f3a19d820b`，本人任职可从受保护接口查询，MFA 配置见[操作说明](login-mfa-development.md)。
