# 显式启用员工入职管理能力

本手册描述开发／测试环境的受控配置操作。安装新版本、启动服务或执行迁移不会给已有管理员自动增加权限。生产环境的管理能力变更流程仍需单独设计和验收。

## 固定角色版本

`role:stage-administrator@1` 保留原有动作、`stage-administration:builtin-v1` 发布标识和内容摘要：

- `administration.people.read`
- `administration.assignment.grant`
- `administration.assignment.revoke`
- `administration.audit.read`

revision 2 使用 `stage-administration:builtin-v2`，在上述四项基础上仅增加：

- `administration.people.create`
- `administration.people.activation.reissue`
- `administration.people.activation.revoke`

这次升级不修改普通角色授予清单，不创建第二位管理员，不代表员工已经激活，也不替代管理员每次 HTTP 操作的近期 MFA 和当前权限验证。角色定义的模板来源仍指向最初 v1 来源；各 revision 的发布标识和摘要分别核验。

## 操作前提

首管已经按[首管管理手册](human-administration.md)明确提名，并由本人通过 MFA 接受，当前 authority、任职、人员、成员经历和登录来源均有效。待接受、撤销或暂停的管理身份不能通过升级恢复生效。

操作由设备 OS 身份执行。必须显式设置正确的 `STAGE_ENV` 和 `STAGE_DATABASE_URL`，且环境为 `dev` 或 `test`；不得把此脚本包装成管理 HTTP 接口。读取当前 authority、任职引用和各自行版本，人工确认本次扩展范围及原因；不要把 role revision 当成任职行版本。

将以下方案保存为当前 OS 用户拥有的普通文件，权限为 `0600`。下列引用为占位示例，版本 `2` 只示范首次接受后的常见状态，实际以当前记录为准。

```json
{
  "environment": "dev",
  "organization_ref": "organization:example",
  "authority_ref": "management:example",
  "assignment_ref": "assignment:management:example",
  "expected_authority_version": 2,
  "expected_assignment_version": 2,
  "target_role_revision": 2,
  "reason": "明确启用员工创建及激活证明重发、撤销"
}
```

## 执行与恢复

在已配置目标开发数据库的受控终端中执行：

```bash
uv run python scripts/administration_capabilities.py --environment dev upgrade \
  --plan /private/path/onboarding-capabilities.json \
  --idempotency-key onboarding-capabilities-change-001
```

操作遵循 OS 管理互斥、TrustState 共享锁、Principal 共享锁、组织排他保护的顺序。在同一数据库事务中安装精确 revision 2、绑定既有管理任职、递增 authority／任职／角色定义行版本及组织授权 epoch，并写入 OS 操作审计。已发布 revision 1 内容不变。

返回结果包含新的 authority 和任职版本、角色 revision、动作集合及组织 epoch。管理员客户端随后重新查询本人管理信息，以当前版本继续提交。普通任职清单和人员自身认证凭据不因本次配置操作而改变。

若连接中断或返回 `result_unknown`，使用原 OS 用户、原环境和原幂等键查询：

```bash
uv run python scripts/administration_capabilities.py --environment dev result \
  --idempotency-key onboarding-capabilities-change-001
```

查询确认历史提交结果，不重新授予权限。若尚无结果，仅允许使用原方案、原键重试；同键不同方案拒绝，不能用新键自动覆盖失败原因。成功后的历史查询在权限后来撤销时仍可供原 OS 操作员恢复审计事实。

版本冲突要求先重新读取当前状态，再明确新的变更意图。此入口只支持 revision 1 到 revision 2 的单次启用；不提供降级、重新提名、重建成员或解除撤销的隐式恢复。
