---
title: P1b.2 人类任职与资源授权实施记录
status: validated-internal-slice
baseline_commit: 8044717
review_date: 2026-10-25
policy_effect: none
---

# P1b.2 人类任职与资源授权实施记录

本轮从冻结提交 `8044717` 推进人类单任职的数据库授权闭环。当前实现只用于可信内部命令与开发管理，不把客户端自报身份当成授权输入，不新增业务写 HTTP，也不宣称整个 P1b/IAM 已完成。真实认证证据到命令重验、ServiceGrant、Agent binding、持久 Delegation、WASM 和能力发布仍未交付。

## 已实现范围

| 部分 | 实际变化与边界 |
|---|---|
| 数据库 schema | 迁移 `f4c7b2a1e903` 从 `d6a3c5f8e902` 增加六张授权表；现有 19 张应用表增至 25 张，不含 Alembic 版本表 |
| 角色能力 | `permission_action`、`role_definition`、`role_version`、`role_version_action`；明确角色版本与 action 集合，内容摘要绑定内置策略版本，角色名称不隐式授予能力 |
| 范围与资源 | `authorization_scope`、`authorization_resource`；组织根复合外键、有限 scope 祖先检查、WorkItem 登记与旧 scope 字段一致性 |
| 旧表扩展 | `role_assignment`、`relation_tuple` 保留数字主键和历史行，增加组织根、稳定引用、成员经历、版本、来源及撤销字段；未映射旧行可空且不进入新 loader |
| 当前授权 | `DatabaseAuthorizationLoader` 只处理 HUMAN、一个选定任职、固定角色版本、WorkItem 与 `viewer_of/owner_of` direct ACL；分类上限固定 `internal` |
| 命令锁与审计 | 可选 coordinator 在组织 guard 前取得 Principal 共享锁；数据库 loader 要求自身/当前事务标记，成员与任职最早到期进入提交前检查；`execution_audit.authorization_json` 保存固定 schema 的授权事实版本 |
| 开发管理 | OS 操作者、dev/test、显式数据库；安装角色版本与明确任职/ACL，撤销 assignment/relation/role_version/scope/resource，journal＋epoch＋变更同事务，可按原键恢复 unknown |

新授权表及旧任职/关系新增根引用使用物理 `organization_ref`；旧身份子表中的 `tenant_ref` 保留兼容。内部执行合同仍使用旧 `tenant_ref` 字段表示唯一部署组织根，不新增组织选择接口。

角色版本内容的不可变性由可信注册器追加规则与 loader digest 校验保证；没有数据库 immutability trigger。直接写数据库的宿主代码属于可信边界，必须遵守注册与撤销协议。新角色 revision 不自动迁移旧任职，也不凭相同 role code 合并权限。

普通业务 handler 不得修改本次授权所依据的安全事实，这是可信宿主的编程合同，并非限制 Python 或数据库访问的安全沙箱。相关 handler 仍运行在可信进程并持有事务对象；新增写路径须经过评审，授权变更只使用专门的协调协议。当前测试和快照重验不能证明任意恶意宿主代码已被隔离。

`relation_tuple` 不作为通用事实注入入口：本切片只接受明确的 direct ACL。任务的 assignee/reviewer/eligibility 需要未来领域权威 loader，不能通过写同名 tuple 获得权限。WorkItem 是内部底座的最小领域对象，不代表任务承诺、交付或验收业务已经完成。

## 并发与失效边界

内部命令使用同一个 loader 实例作为 `coordinator`：先锁 HUMAN Principal `FOR SHARE`，再锁唯一组织 guard，之后加载当前授权和目标 WorkItem。主体撤销通过同一 Principal 独占锁协调；授权开发变更使用共同组织 guard，并在同一事务推进 epoch。loader 不接受上次事务的锁标记，也不接受另一个 loader 的标记。

成员结束、重新加入、任职/关系到期、角色版本撤销、资源停用和 scope 祖先停用，都需当前重新判断。自然到期不依赖 epoch 改变；读取授权时固定的最早截止在提交前再次检查。审计事实版本和 epoch 只是本次判断依据，不能作为以后免于重验的许可。

尚未将身份服务产出的 profile/trust/外部映射/企业来源准入证据接入 `CommandKernel`。本轮 Principal 共享锁与组织授权测试不能替代真实 IdP→业务写入的联合验收。旧 fixture loader 路径仍保留，但它仅供受信任测试组合，不是公开入口的替代实现。

## 迁移与验证记录

2026-10-25 完成以下检查，范围限定于本轮内部人类授权切片及既有功能回归。旧 reviews 的数量和通过范围保持原样，跳过用例不计为通过；SQLite 不替代 PostgreSQL 的锁顺序与竞争证明。

| 验证项 | 实际结果 |
|---|---|
| SQLite 全套 | 567 passed，26 skipped |
| PostgreSQL 17＋Redis 全套 | 596 passed，5 skipped；跳过项均为需显式启用的在线冒烟 |
| PostgreSQL 命令/撤销竞争 | 6 个真实多连接场景通过，覆盖双方先后取得锁的顺序 |
| 在线系统冒烟 | 5 passed，独立启用后执行 |
| 真实 Keycloak → v1 身份 API | 18 项检查通过；仅验证既有身份链，不代表 IdP → 业务命令已闭环 |
| 静态与合同 | Ruff、127 个文件格式检查、124 个源文件 mypy、OpenAPI 合同与 `git diff --check` 通过 |
| 数据库迁移 | 独立空 PostgreSQL 库完成 upgrade head → check → downgrade base → upgrade head → check 五项检查 |
| 旧数据与序列 | 独立 PostgreSQL 库以默认序列连续生成 ID 1 → 升级后 2 → 降级后 3 → 再升级后 4，旧行和序列保持；两次 head 均通过 schema check |
| CLI 与内部执行 | 隔离 PostgreSQL 中实际子进程执行 install/replay、内部命令与审计、revoke/result，撤销后读取拒绝；未在开发库安装示例业务授权 |
| 开发服务 | Stage 重启后，Stage/PostgreSQL/Redis/隔离测试 IdP 四个服务均 active |

迁移往返使用 `stage_test_authz_migrations`；历史行/默认序列留存使用 `stage_test_authz_retention` 与 [check_authorization_migration.py](../../scripts/check_authorization_migration.py)；真实 CLI 子进程闭环使用 `stage_test_authz_cli`。这些均为独立测试库，不使用开发库做重置或 downgrade。PostgreSQL downgrade 使用原生 ALTER 保留旧表及序列；完整往返验证不表示在真实数据上推荐降级回滚。

开发库在迁移前保存仓库外备份 `stage-before-authorization-p1b2.dump` 和逐条快照 `before-authorization-p1b2.json`。应用 `f4c7b2a1e903` 并通过 Alembic check 后，旧数据逐条比对不变；六张新增授权表为空，没有给现有成员自动授予角色、ACL 或业务 grants。应用表总数核验为 25 张。备份仅证明本轮保留回退材料，不替代生产恢复演练。

文档完成两轮现状/目标交叉核对；最终 15 份变更 Markdown、202 个本地链接与代码围栏检查通过，`git diff --check` 通过；历史证据未改写。

## 下一步门槛

1. 接入经过验证的认证证据到命令上下文，按 TrustState → Principal → 组织 guard 的统一顺序重验映射、profile、来源准入与有效截止，再讨论开放受保护业务写 API。
2. 按第一个实际服务/Agent 消费场景建设 ServiceGrant、固定 Agent 登记/承载关系和有限 Delegation；不能用当前 HUMAN 任职路径模拟服务身份。
3. P1c 才增加能力/模块版本、WASM runner 和签名准入；不以本轮授权表为由提前建设通用 DSL、动态 Python 插件或持久工作流。

操作说明见[授权开发操作](../authorization-development.md)，完整设计见[身份表结构](../identity-schema-design.md)与[P1b 实施入口](../p1b-implementation-entry.md)。
