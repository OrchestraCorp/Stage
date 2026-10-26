# 首管引导与有限任职管理

当前合同见[首个管理切片](../../docs/human-administration-first-slice.md)，请求定义以[OpenAPI](../../contracts/openapi.yaml)为准。本手册只管理已经存在的人员、成员和普通角色版本，不创建登录账号或员工激活邀请。

## 1. 前置条件

1. 数据库已通过备份和迁移到 `a7d4e2c9f501`。迁移只新增空的 `management_authority` 表，不自动创建管理员。
2. 部署拥有唯一组织根，人员和当前非自动到期成员经历已显式登记；认证映射、profile 与企业准入有效。
3. 组织登录策略为 required，精确 profile 的 ACR/AMR 要求密码＋TOTP。管理入口另要求最近300秒认证，不接受刷新延长认证时间。
4. 普通角色 revision、内容摘要和 scope 已由受控登记建立。本轮无角色发布、修改可授清单或多人管理员管理 API。

`STAGE_ENV`、显式 `STAGE_DATABASE_URL` 和命令行 `--environment` 必须一致。新提名命令可用于已配置好的 prod 基础，但现有 dev/test 身份安装器仍不支持生产初始化；不能仅凭本命令宣布生产安装、恢复或备份演练完成。

## 2. 本机提名

建立仅操作者可读的0600 JSON计划，字段为：

| 字段 | 来源或限制 |
|---|---|
| environment / organization_ref | 明确部署登记，不能由浏览器选择 |
| principal_ref / membership_ref | 已核验人员及其当前、非自动到期的成员经历 |
| external_identity_ref / profile_ref / profile_version | 本人实际使用的精确认证映射与来源版本 |
| expected_version | 首次为0；未接受时重提名必须读取当前版本 |
| expires_at | 有时区的时间，最多未来一小时 |
| reason | 必填的人类可读提名原因 |
| grant_rules | 1～32项精确 role_ref、role_revision、content_digest、scope_ref、max_duration_seconds |

内容摘要必须来自已登记的角色版本；scope按精确值匹配，不自动包括子范围。每项最长授予期限为1～31536000秒。不能用管理角色作为普通可授角色。

在已经安全加载部署环境的受信任终端中执行：

```bash
uv run python scripts/administration_bootstrap.py --environment dev nominate \
  --plan /absolute/private/first-administrator-plan.json \
  --idempotency-key first-administrator-nomination-1
```

响应返回提名及管理任职引用，此时权限尚未激活。保存原计划、键和结果。网络或提交结果未知时先查询原键：

```bash
uv run python scripts/administration_bootstrap.py --environment dev result \
  --idempotency-key first-administrator-nomination-1
```

未接受的提名允许按预期版本重提名，旧 suspended 任职被撤销。只要 `accepted_at` 已写入，即使管理任职后来失效，任何新初始化计划都拒绝；旧键仅返回旧记录，不重新授予。不能以直接清空接受记录作为恢复方案。

## 3. Maestro 接受与管理

使用系统浏览器登录。出现 MFA_REQUIRED 时要求重新完成认证，不能仅刷新 token；原生登录可以通过 `prompt=login` 和 `max_age=0` 请求新认证。

| 方法与路径（均在 `/api/v1/administration` 下） | 客户端用途 |
|---|---|
| GET `/me` | 发现本人的首管提名或管理任职、权限版本及可授清单；普通人员为 authority=null |
| POST `/activation` | 本人接受首管职责；这是管理权限接受，不是员工账号激活 |
| GET `/people`、`/people/{principal_ref}` | 当前active/suspended成员的最小人员标识与状态 |
| GET `/people/{principal_ref}/assignments` | 查看当前成员经历下的普通任职事实，包括已撤销及角色已停用的记录 |
| POST `/people/{principal_ref}/assignments` | 按精确清单授予有限任职，必须填写成员版本、权限版本、期限及原因 |
| POST `/people/{principal_ref}/assignments/{target_assignment_ref}/revoke` | 撤回有限普通任职，填写任职版本、权限版本及原因 |
| GET `/operations/{idempotency_key}` | 原人员以当前管理权限恢复自己的原操作结果 |
| GET `/audit` | 本管理切片的人类操作审计，含目标、原因、request_id及结果 |

所有入口带 Bearer 和精确 profile_ref/profile_version；除 `/me`、`/activation` 外，再带 `/me` 发现的管理 assignment_ref。不能把普通业务任职作为管理任职，也不能把管理任职作为工作项业务权限。

写入带 Idempotency-Key；重试保持原动作、目标、任职、版本、期限和原因。版本冲突需要刷新并由人员确认新意图；COMMIT_OUTCOME_UNKNOWN 使用响应 Location 和原键恢复，404不证明旧请求必然未提交。所有响应包含 no-store。幂等恢复不重复执行，也不会返回新的权限票据。

分页默认20、最大50，以返回的 next_after 继续；没有跨页快照承诺。任职列表的 status只表示持久行状态，不能据此判断某个对象的有效权限。自然到期的active记录需先显式撤回再重新授予；对已经结束的成员可以收回旧任职，但不能借新授予复活旧经历。停用或损坏角色／scope后的清理另按受控修复处理。

## 4. 范围与恢复责任

首版仅支持一名首管，普通接口不能自授、自撤、授予／撤销管理角色，也不能改动其可授清单。全组织人员可见性是当前明示范围，不提供没有真实归属模型支持的部门人员过滤。

管理者账号、因素或成员资格因其他受控操作不可用时，当前没有生产应急恢复或管理员转交端点。后续恢复／离职／多管理员切片必须定义责任核验、末位管理员不变量和具体审计，不能通过重开bootstrap兜底。本机OS操作者仍是数据库部署信任边界；HTTP用户永远不能声明自己是该操作者。

Maestro页面、首管确认体验、人员可读名称和后续员工激活／离职共同验收另行推进。合成HTTP／浏览器证据不替代真实客户端验收。
