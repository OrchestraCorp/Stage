---
title: Stage 代码状态复查与下一轮开发顺序
status: fixes-verified-next-round-scoped
review_date: 2026-10-26
policy_effect: none
---

# Stage 代码状态复查与下一轮开发顺序

本轮以当前工作区源码、自动化检查和开发设备为依据，复查身份/HTTP、授权/事务/结果恢复、Runtime/provider、持久投递、schema 与 CI。上轮[文档定位复查](documentation-alignment-2026-10-25.md)继续约束范围；没有把未实现的 MFA、任职发现、正式人员管理、WASM 或组合目录计入交付。

审查开始时 HEAD 为 `8044717`，工作区包含后续 P1b.2/.3、收尾与文档修改，尚未形成新提交。结果针对工作区，不冒充远端 CI 或已合并版本的验收。原始性能记录与历史验收保持原批次，不能因本轮修改 Runtime 而声称旧源码摘要仍等于当前代码。

## 1. 已确认并修复的问题

| 发现 | 触发与影响 | 修复及回归 |
|---|---|---|
| P2：无关可选插件依赖环污染必需组件就绪 | 一个正常 required 组件和互相依赖的 optional A/B 同时加载，`await_ready(required)` 在正常组件启动前误报 DependencyCycle | [_ready/_cycles](../../app/runtime/host.py) 只检查 required 可达依赖；全局 snapshot 仍报告其它环。必需的传递依赖环继续失败，handle/key 两种调用均覆盖 |
| P2：成功换代保留历代服务对象 | 每次 provide 的 binding 留在 record 列表，成功 restart 后已结束代次的 value 仍被强引用；长期换代可积累对象内存 | 仅在 drain 与 effect cleanup 成功后释放旧 binding 列表；失败/blocked 保留对象与诊断。weakref 验证重复重启后旧对象可回收，终态诊断仍存在；没有据此声称发现数据库连接泄漏 |
| P2：非法外部回执未进入 unknown | sink 的 `receipt_ref` 含 NUL/孤立 surrogate，SQLite 可能误记 sent 或编码报错，PG 无法保存该文本而使 Worker 故障 | [_receipt_matches](../../app/delivery/worker.py) 拒绝这些标识，沿现有 `receipt.invalid → unknown` 记账；发送和对账均覆盖，合法中文/emoji 精确保留，不自动重发 |

Runtime 新增 [6 个回归用例](../../tests/runtime_core/test_review_20261026.py)，修前 3 failed / 3 passed，修后 Core＋Application 定向 136 passed。投递新增边界进入 [Worker 测试](../../tests/delivery/test_worker.py)，修前复现非法 sent/编码异常，修后投递定向 57 passed；这些定向数字和后续全量有重叠，不累计。

另标明四处旧草案类型的代码注释：`app/core/audit.py`、`outbox.py`、`idempotency.py` 和 `app/features/work/commands.py`。当前应用/测试中没有使用这些类型的业务调用链；它们不替代已实现的 Execution Kernel、OutboxIntent 或可信身份。保留兼容类，不新增删除或运行时弃用行为。

## 2. 当前代码可以继续沿用的部分

- 身份路径核验固定 issuer/audience/client/kind、签名、时间与精确映射，随后检查当前组织来源/成员经历；service 凭据不能充当人员命令依据。身份定向 109 passed、3 项 PG 专属场景按该轮配置跳过；非有限及超大 NumericDate 探针均拒绝。
- 公开人员命令通过真实 evidence、单任职、固定角色版本、scope/直接 ACL 及可信属性进入短事务；提交前重验身份与时效。没有发现新的可复现越权、跨人结果读取或幂等重复提交问题；这不构成对任意未来 handler 的证明。
- 当前 HTTP → WORK/IDENTITY → STORAGE 调用保留租约直到事务/Session 清理；状态与事实仍在 PostgreSQL。Core 不持当前人员身份，数据不因组件卸载删除。
- 构造时固定 CommandSpec 集合与静态 FastAPI 路由足以继续当前人员切片。没有必要先建设动态能力注册、蓝图平台、持久流程或多节点。
- schema 为 25 张应用表，head `f4c7b2a1e903`；本轮没有迁移、公开接口或权限默认授予变更。

## 3. 开发接缝与优先级

| 优先顺序 | 下一轮工作 | 现有代码接缝与完成依据 |
|---|---|---|
| 先固定审查版本 | 将通过审查的工作区整理为可追踪提交 | 当前大量 P1b 代码仍未提交；已有 GitHub/GitLab CI 配置不等于远端已验证这份工作区。历史 evidence 不覆盖后续修改 |
| 1：受保护的本人任职发现 | 小型查询入口＋有界的本人任职投影 | 当前 `/identity/me` 仅有主体/成员，工作项接口却要求提前提供 assignment_ref。复用 `authenticate_human`、`acquire_human_locks`、`validate_human` 和组织 guard；限定当前本人及本次 membership，拒绝跨人/旧经历，结果不成为权限票据 |
| 2：登录 MFA 的受控配置与强制验证 | 冻结一个实际 IdP profile、强度含义和适用新鲜度，完成真实因素与客户端正反向验收 | 当前只保留 auth_time/acr/amr 和 required_acr 精确比较。ProfilePlan/bootstrap 没有交付 MFA/新鲜度配置或已有 binding 的受控修改路径，需要带预期版本、幂等、审计、共同锁序的窄管理操作；不能要求手工 SQL |
| 3：Maestro 实际人员闭环 | 系统浏览器、回调/凭据保管、任职选择、刷新、原 key 恢复、注销与本地撤权 | [联调合同](../maestro-stage-p1b-handoff.md)已有服务器接缝，实际桌面与强 MFA 仍未验收；可和上述服务端工作按稳定合同并行，不把脚本登录当桌面完成 |
| 再进入 P1c/P1d | 受控 WASM、具体意图批准、签名准入与资格评估 | 复用已验证身份与短事务协议；登录 MFA 不依赖 WASM 或 Stage 批准 ceremony。具体因素/批准 profile 未在本轮选定 |

本人任职发现不应复用要求“先选定任职才能读取 WorkItem”的 loader。若引入公共查询授权入口，应只封装实际需要的锁序、证据重验和 deadline，领域投影继续由对应服务负责；不要为了隐藏 `HumanWorkService.get_item()` 对 `kernel._authorize` 的私有调用而先做通用策略框架。

MFA 必须覆盖全部仍有效的准入路径。API 可以选择已登记的 profile；只增加一个强 profile 而保留能访问同一受保护业务的弱 profile，不构成强制 MFA。还应验证未知强度、仅单因素、取消/失败、新鲜度到期，以及刷新和账户恢复不能无依据抬高强度。具体第二因素和 IdP 映射先明确，本轮不擅自选 TOTP/Passkey，也不建设完整 IAM 控制面。

## 4. 不应抢先扩展、但要保留的限制

| 限制 | 对未来开发的约束 |
|---|---|
| 开发授权安装捆绑角色发布和任职授予 | 当前 `InstallAuthorizationPlan` 必须新建 role revision，不能仅给新人员授予已有 revision。正式最小人员管理出现时再拆开受控“发布角色”和“授予任职”，不可用复制角色版本绕过产品缺口 |
| 旧结果恢复需要旧 CommandSpec | `get_result`/`get_result_by_key` 仍查命令描述。当前停用功能必须保留旧读合同及必要实现；真正动态卸载前再分离执行与历史结果读取描述 |
| 组织 guard 串行 | 保留已验证写入协议；原[并发基线](p1b-human-work-baseline-2026-10-25.json)描述对应旧源码批次，不承诺生产容量。取得实际负载目标后再评估细粒度锁；增加 provider/worker 数量不能解除该锁 |
| 健康抖动与换代部分共用路径 | 当前先保证依赖安全；只有可观测的重建开销或可用性需求出现后再做故障退避/换代策略拆分 |
| 可信 Python 与开发控制面 | 插件和 handler 不是安全沙箱；正式 sink/authorizer、人员管理和敏感批准仍需各自接入。Runtime 修复不意味着可上传任意插件 |
| 迁移与协议证据的自动化范围 | CI 有静态、合同、PG 空库迁移及回归、Redis/Core 场景检查；真实 IdP 测试需显式 opt-in，保留旧行/serial 的 PG 脚本已有独立验证但未接入常规 CI。正式持续集成前应接入独立测试库门禁，不能拿 ORM create_all 测试代替迁移 |

## 5. 本轮验证

| 检查 | 结果与范围 |
|---|---|
| 本地 SQLite 快测配置 | **730 passed，30 skipped**，211.85 秒；不作为 PostgreSQL 锁证据 |
| PostgreSQL 17＋Redis 完整回归 | **762 passed，6 skipped**，169.06 秒；跳过 5 项在线只读冒烟和 1 项需显式凭据的真实 IdP 写入，后续分别执行 |
| Core 场景映射 | **46/46**，来自上述真实 PG 回归的 JUnit，见[本轮机器证据](code-runtime-conformance-2026-10-26.json)；不增加原规格场景数，也不冒充本轮重跑 TS 参考 |
| 真实 Keycloak → HTTP → PG 写入 | **1 passed**，3.40 秒；使用独立临时服务和测试数据库，包含刷新重放、恢复、本地撤权和 IdP 注销边界 |
| 常驻开发服务 | 重启后 `/ready` 正常；实际 OpenAPI 与当前合同相同；只读冒烟 **5 passed**，真实 IdP 协议 **18 项通过**；systemd active/running，NRestarts=0 |
| 静态与合同 | Ruff、142 个文件格式检查、139 个源文件 mypy、OpenAPI 合同、`git diff --check` 全部通过 |
| 数据库 schema | Alembic current 为 `f4c7b2a1e903` head，check 无差异；本轮无迁移 |
| 文档 | 全仓本地链接、Markdown 锚点、围栏及当前 Mermaid 内联/图源一致性通过 |

以上测试批次有覆盖重叠，不累计成额外功能数量。测试库重置仅用于明确授权的独立 PostgreSQL 测试库；不向常驻开发库安装角色、任职或工作项。本人任职发现、登录 MFA 配置和强制策略、正式人员管理均仍未实现；当前通过数不能解释成这些功能完成。
