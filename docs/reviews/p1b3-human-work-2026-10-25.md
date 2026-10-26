---
title: P1b.3 人员认证到工作项元数据命令实施记录
status: implemented-and-verified
implementation_status: p1b3-human-work-verified-full-p1-pending
review_date: 2026-10-25
policy_effect: none
---

# P1b.3 人员认证到工作项元数据命令实施记录

用户确认恢复开发，并明确选择“人员修改已有工作项的标题和说明”作为首条最小业务操作。本切片遵守[人员—公司—人员](../people-company-collaboration.md)模式：只有经过认证的人员及其当前单任职取得业务权限，Agent 是受监督的工具。没有增加独立 Agent 主体、ServiceGrant、承载绑定或 Delegation，也没有任务领取、交付、验收或人工审批流程。

本文记录当前工作区的代码交付与实际验收结果。开发环境、实际测试 IdP 和生产接入分别留证，不能从一个成功示例推导整个 P1 或 P2 已完成。

## 1. 本轮交付

```text
真实人员 access token
→ 事务外验证固定 profile / issuer / audience / client / 签名
→ 当前映射、组织来源准入及成员核验，签发短期内部认证证据
→ 明确选择一个属于该成员经历的任职
→ TrustState SHARE → Principal SHARE → 组织 guard → 角色 / scope / 资源
→ 固定角色版本 action + owner_of + draft/open + 预期版本
→ 只修改标题、说明和对象版本
→ 重验认证事实、授权事实和当前截止时间
→ PostgreSQL 原子提交业务结果、命令结果与审计
```

`HumanAuthenticationEvidence` 保存经过验证的引用、版本、epoch、认证 profile 摘要及截止时间，不保存 bearer token。它只在可信宿主内传递，不作为客户端 DTO 或可重用权限票据。`ExecutionContext` 由服务端构造，认证人员、组织根与有效主体必须一致；请求只能选择一个 `assignment_ref`，不能自报 actor、组织、权限集合或 Agent 身份。

`AuthenticatedAuthorizationLoader` 将认证证据重验与 `DatabaseAuthorizationLoader` 组合；同一实例作为命令的 loader、coordinator 和提交前身份 validator。IdP/JWKS 网络调用发生在业务事务外，锁后及提交前核验不访问网络。请求全程有有限预算，组件租约只保证依赖存活，不授予业务权限。

公开路径使用 `work.details.update@1` 和稳定动作 `task.metadata.update`。新动作必须出现在任职固定的角色版本中，当前对象必须有同成员经历的 `owner_of`，scope 链、状态、分类与版本同时满足策略。读取及操作结果恢复使用当前 `task.view`。不自动合并多个任职，也不按角色名称补齐能力。

这条元数据命令没有外部副作用，不生成通知或 Outbox 消息。现有可靠投递底座继续保留自己的验收边界；本轮不默认装配生产 sink。

## 2. HTTP v1 合同

正式结构由[OpenAPI](../../contracts/openapi.yaml)与路由/契约测试共同固定。

| 路由 | 作用 | 必要选择与输入 |
|---|---|---|
| `GET /api/v1/work-items/{work_item_id}` | 读取当前获授权的工作项投影 | Bearer；query `profile_ref`、`profile_version`、`assignment_ref` |
| `PUT /api/v1/work-items/{work_item_id}/details` | 完整替换标题与说明 | 同上；`Idempotency-Key`；body `title`、`description`、`expected_version` |
| `GET /api/v1/work-items/{work_item_id}/operations/{idempotency_key}` | 查询当前人员的原命令结果 | 同一认证选择与当前任职；路径携带原幂等键 |

`title` 去除首尾空白后为 1～256 字符；`description` 必须显式提供，文本最长 20,000 字符或 `null`；`expected_version` 是正 int32。字段严格校验，拒绝额外身份、权限、状态、scope、分类与确认字段。幂等键为 1～128 个受限 ASCII 字符，首字符为字母或数字，其余允许字母、数字、`.`、`_`、`:`、`-`。所有成功和错误响应使用 `Cache-Control: no-store`。

编辑和结果查询返回 `execution_id`、`status: succeeded`、`result`、`replayed`、`completed_at`；`result` 包含 `resource_ref`、`title`、`description`、`lifecycle`、`classification`、`version`。刷新同一人员的 token 不改变原命令幂等身份；PUT 重放重验当前认证和读权限，不覆盖原认证/授权审计，改变任职或命令输入会造成幂等冲突。GET 原 key 结果恢复允许同一人员选择另一个当前具有 `task.view` 的任职，不要求原任职仍然存在；它不会重新执行原命令。

| 状态 | 边界 |
|---|---|
| 401 | 凭据缺失、无效或认证证据/凭据到期，带 Bearer challenge |
| 403 | 当前身份、成员、认证来源或业务授权不满足；不可访问资源不暴露其内容 |
| 409 | 对象版本、可编辑状态/版本上限或幂等输入冲突 |
| 404 | 原人员/目标/命令下没有可返回的操作结果；不能据此跳过原幂等键恢复协议 |
| 422 | 参数、额外字段、类型、范围或不支持的输入 |
| 503 | 服务暂不可用或请求预算到期；提交结果不确定时 `COMMIT_OUTCOME_UNKNOWN` 携带原 key 的相对查询 Location |

收到提交未知或网络中断后，保留原幂等键和原输入，用当前有效人员凭据查询原操作，必要时按相同 key/输入重试。不得换键制造第二个业务动作。Location 不包含 token，也不提供绕过当前鉴权的结果读取能力。

## 3. 持久事实与开发管理

本轮没有新增表或迁移：应用模型仍为 **25 张表**，Alembic head 保持 **`f4c7b2a1e903`**。沿用 `work_item`、`command_execution` 和 `execution_audit.authorization_json`；审计保存原认证证据的最小白名单快照和固定角色/关系/授权版本，不保存 token、外部 subject 或虚构的人工批准。

原角色版本保持原 action 集合，不自动获得 `task.metadata.update`。管理员需要显式安装具有该动作的新角色 revision、对应任职及真实对象 ACL；旧任职仍指向旧 revision。开发工具仅限 dev/test、本机 OS 操作者与虚构测试数据，不证明其具备正式企业治理批准。详见[授权开发操作](../authorization-development.md)。

没有公开创建工作项、人员邀请、成员/任职授予、角色发布或 ACL 管理 HTTP。测试工作项必须通过明确的开发 fixture 建立，角色/资源登记通过受控管理流程完成；服务启动、登录或首次访问不会自动创建成员或授予角色。

## 4. 验证计划与结果

| 验证项 | 本轮最终结果 |
|---|---|
| SQLite 完整回归 | **676 passed，30 skipped**；207.17 秒，不代表 PostgreSQL 锁语义 |
| PostgreSQL 17＋Redis 完整回归 | **708 passed，6 skipped**；163.05 秒，跳过 5 项 opt-in 只读服务冒烟和 1 项 opt-in 真实写测试 |
| 真实 Keycloak → Stage 人员读/写/恢复链路 | 独立可丢弃 PG 库中 **1 passed**；实际迁移、授权码＋PKCE、临时 loopback Uvicorn、刷新重放、原 key 恢复及任职撤销；补强迁移子进程取消清理后重跑 **1 passed**（3.23 秒），不是两项独立用例 |
| 人员命令与 PG 竞争定向组合 | 11 项认证命令＋3 项 PG 锁竞争＋1 项上述真实写测试，合计 **15 passed**；与全量/独立结果有重叠，不累计成额外覆盖 |
| 在线健康、就绪、只读冒烟 | 服务重启后 **5 passed**；常驻开发服务真实身份协议检查 **18 项通过** |
| 静态与合同 | Ruff 通过；138 个文件格式检查、135 个源文件 mypy、`scripts/check_contract.py` 通过；OpenAPI 合同测试包含于全量回归 |
| 开发 schema 与服务 | Alembic current 为 `f4c7b2a1e903` head，check 无差异；systemd active/running，`NRestarts=0` |
| 开发库无隐式赋权 | 真实人员调用新增 GET/PUT 因无任职返回 403 且 no-store；`role_definition`、`role_assignment`、`work_item`、`command_execution` 均仍为 0 行 |
| 文档与图 | 26 份新增/变更 Markdown、320 个本地链接与代码围栏通过检查；当前 Mermaid 源与内联图一致，两个图源渲染通过，目标 SVG/PNG 已更新 |

全量测试使用明确可重置的独立测试库；真实写测试不操作 `stage` 开发库。跳过不计通过；上述结果不宣称生产 IdP、正式员工权限或 Maestro UI 已验收。真实写复现参数及数据清理范围见[开发运行手册](../../deployment/dev/README.md#独立测试库的真实人员写入验收)。文档收口后的链接、图源/导出与最终 diff 检查已完成。

验收须覆盖：

- 服务 token 不能发起人员命令，请求体/额外 query 不能伪造人、组织、任职能力或确认。
- 明确单任职、旧角色不增权、非 owner 与非 draft/open、scope 与预期版本不匹配均拒绝。
- profile/trust/映射、主体、组织来源准入和成员变化阻断旧证据；重新加入不复活旧成员经历。
- PostgreSQL 两连接证明“撤销先提交→旧证据命令拒绝”和“命令先持锁→撤销等待提交”；trust 锁先于 principal 与组织 guard。
- handler 内身份映射事实误变更、认证在 handler 或最终异步重验期间到期时整笔事务回滚。角色/ACL 等授权事实禁止由业务 handler 修改，仍属于可信代码约束。
- token 刷新后的原 key 重放不重复写、不改原审计；COMMIT 成功但回执丢失可按原 key 恢复。
- 锁等待、取消、请求预算与组件停止均释放连接和调用租约；未知结果不伪装成确定未提交。

## 5. 后续边界

本轮完成的是 P1b 的最小受控人员操作，不代表完整 IAM、正式企业身份、生产接入或 P2 任务业务。Maestro 继续在本地按版本化合同联调；实际桌面授权码＋PKCE、生产 IdP、完整 I1 验收仍按各自证据推进。之后按一期顺序建设 WASM、签名准入和资格评估，不插入独立 Agent 身份平台、通用工作流 DSL 或多节点控制面作为前提。
