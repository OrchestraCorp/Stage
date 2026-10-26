---
title: Stage 架构审计 P2/P3 整改与复查
status: implementation-and-review
date: 2026-10-26
policy_effect: current-engineering-boundaries
---

# Stage 架构审计 P2/P3 整改与复查

本轮落实[整体工程审计](stage-engineering-audit-2026-10-26.md)的 A–H：五项 P2、三项 P3。这里是发现的优先级，**不表示产品路线图第二期、第三期已经交付**。保留企业私有化、人员监督 Agent、模块化单体、PostgreSQL 权威和明确的授权/事务边界。

## 1. 逐项结果

| 原发现 | 本轮落实 | 验证与边界 |
|---|---|---|
| A · 公共受保护业务入口 | [HumanOperations](../../app/providers/human.py)统一身份、截止、lease、ExecutionContext 和完整 kernel 装配；[CommandKernel.read](../../app/execution/kernel.py)在同一受管事务内先授权、读取、披露前重验；通用 command provider 补齐 identity_validator | 真实工作项三个入口已复用；读取跨身份/任职/关系到期、撤权、回调事务越权和异常回滚有回归。没有增加通用 DSL 或第二个假业务域 |
| B · 认证 HTTP 过度集中 | 原 1,211 行文件变为[30 行组合入口](../../app/api/native_auth.py)，分为 common/browser/logout/oauth/account 五个同层职责模块 | 完整 OpenAPI 对象保持一致；已有 HTTP、Authlib、Chromium、Cookie/Origin/CSP 回归覆盖；共享验证没有复制 |
| C · Chromium 未进 CI | GitHub/GitLab 增加独立 PostgreSQL＋锁定 Playwright 1.58.2 的浏览器作业；[门禁](../../scripts/check_browser_acceptance.py)拒绝缺例、跳过、失败、旧报告和非零退出 | 本机按相同命令实际执行两个场景；仅上传裁剪后的名称/时间/状态。远程托管 CI 是否执行以推送后的平台记录为准 |
| D · 缺少可复现交付冻结 | [冻结工具](../../scripts/freeze_release.py)只接受干净提交，从固定 Git revision 归档/构建，绑定 uv/browser 锁、迁移 head、Python/uv 与制品摘要 | 收尾提交包含全部已审查身份/OAuth/整改代码和验证记录；独立源码/包制品与最终隔离核验绑定同一 revision；wheel 现在包含启动检查需要的 migrations，并接受独立目录门禁。开发工作区挂载与冻结制品分别识别，见[运行方法](../../deployment/dev/release-freeze.md) |
| E · 登录扩大 guard 争用 | UserInfo profile 投影合为一次锁内读取；新增[独立容量脚本](../../scripts/benchmark_mixed_auth_work.py)，测四类混合操作与 16 人真实密码＋TOTP 登录突发 | 记录每类延迟、SQL、guard 获取/持有时间与 1/4/8/16 并发曲线；保持安全锁序与撤权一致性。见[容量报告](p2p3-capacity-2026-10-26.md)及[原始基线](p2p3-capacity-2026-10-26.json) |
| F · 公共协议藏在专用模块 | [app.control](../../app/control/operations.py)提供本地管理事务/互斥/环境/日志/重放/unknown；[current_session](../../app/native_auth/current_session.py)承载当前事实验证；protocol 承载纯协议合同；配置改名 AccessTokenProfile | 管理八个函数经中立命名替换后 AST 与原实现一致；历史 journal 身份/摘要保持。保留 KeycloakProfile 兼容别名；没有构造通用认证插件系统 |
| G · Runtime 历史无界 | 成功卸载释放 setup/config/inject/context/scope，host 只留默认 256 条摘要；observations 同样有界并报告省略数 | GC、20 轮替换、父子关系、旧句柄、失败清理与参考行为均验。failed/blocked 所有权继续保留；调用者显式持有旧 handle 时仍保留 base，用于兼容 replace 的原隔离域/继承配置 |
| H · 测试文件变共享库 | 共享 builders、时钟、HTTP fixture、迁移和 PG 观察助手移到[tests/support](../../tests/support/README.md)，新增 AST 导入边界；共享 PG＋xdist 在连接前拒绝 | 支持包无反向 test 导入及循环；多个独立 pytest 进程仍须串行或分配不同数据库，未伪装成跨进程锁 |

## 2. 复查结论

两轮独立只读复查没有留下新增、需要修复的 P2/P3 架构问题。制品复查另外发现旧 wheel 未携带启动探针所需的 migrations，已补充包配置与 CI 独立目录校验；正常 wheel 可导入且 head 正确，人为移除 migrations 的 wheel 被门禁拒绝。重点核对了以下不变量：

- 人员 kernel 的事实加载、锁协调、身份重验三个接口同时装配；受保护读取在回调前后校验当前权限，回调不能提前提交或结束根事务。
- 管理修改、操作 journal 与结果同事务；原 actor/type/key 重放、环境隔离、提交未知恢复保留原行为。
- 身份业务路径仍按 trust SHARE→principal SHARE→组织 guard；账户修改路径仍按 trust SHARE→principal UPDATE→组织 guard→account。当前会话函数只读事实，不替调用者重排锁。
- UserInfo 在同一账户锁域物化结果；数据库等待后的 token/session 到期继续重验。refresh 没有制造新的 MFA 时间，撤销和客户端版本失效仍立即参与判断。
- Runtime 清理失败或未排空时不能归档；成功记录移出不改变依赖排空与替换范围。成功卸载后 `handle.context` 为 None，全局 close 只报告当次仍管理的 owner，不提供无限历史累计清单；旧 handle 自身仍能报告其最终身份/状态。
- 浏览器 CI 不把跳过当通过；冻结工具拒绝脏工作区，制品从提交归档构建，未混入工作区覆盖或私有环境文件。

这次整理使新增业务可以复用明确的安全入口，认证 HTTP 和共享协议的修改范围也更小。仍保留一个稳定底座上的受信业务组件组合；动态插件 API 不代表任意不可信代码可以进入服务进程。

## 3. 验证记录

最终机器记录见[p2p3-validation-2026-10-26.json](p2p3-validation-2026-10-26.json)。各次运行独立记录，不把重跑或有交集的定向组累加为功能数量。

<!-- final-validation:start -->
| 检查 | 最终结果 | 说明 |
|---|---:|---|
| PostgreSQL＋Redis 全量 | **1479 通过 / 9 跳过** | [逐例摘要](p2p3-postgresql-tests-2026-10-26.xml)；外部 IdP/浏览器/在线探针按各自环境运行 |
| SQLite 开发快测全量 | **1428 通过 / 52 跳过** | [逐例摘要](p2p3-sqlite-tests-2026-10-26.xml)；不代表线上需要 SQLite |
| 真 Chromium CI 门禁 | **2 场景通过 / 0 跳过** | [摘要](p2p3-chromium-tests-2026-10-26.xml)；恢复重绑及 SSO/同意/form_post/跨站退出 |
| 开发设备重启后检查 | **5 探针＋9 原生认证＋6 浏览器登录** | 真实 PG/Redis；密码/TOTP/PKCE、业务身份、刷新/退出 |
| Runtime 映射 | **46/46** | 最终 PostgreSQL JUnit 逐场景映射 |
| Cordis 固定参考 | **34/34、35/35、9/9** | [参考记录](p2p3-runtime-reference-2026-10-26/manifest.json)：观察、共享片段、预声明差异分别计数 |
| 静态与合同 | 全通过 | Ruff、258 文件格式、254 文件 Mypy、OpenAPI；wheel 独立导入/迁移 head 正例和资源缺失反例 |
| 容量与一致性账 | **2048 操作＋16 登录，零错误** | 512 条命令、审计、版本增加相符 |

最终两套全量期间 app、tests、migrations 源码保持一致。制品复查只补了打包配置、CI 与两个发布校验脚本，随后单独完成静态及 wheel 正反例验证；未以运行中的旧应用代码代替新代码验收。两条 Authlib 废弃提示来自互操作测试客户端，本轮无测试失败。
<!-- final-validation:end -->

首轮 PostgreSQL 全量为 1,478 通过、1 失败、9 跳过。唯一失败是新容量测试将实际表 `execution_audit` 写成 `audit_record`；已经修正，并在独立 PostgreSQL 复测。业务服务实现没有因此修改。最终全量使用稳定后的文件集重新执行，不以旧进程载入的测试代码冒充新版本验证。

共享管理协议定向组 222 通过 / 4 个 PG 专项跳过；独立审计的恢复/环境/读取/门禁组 58 通过 / 4 跳过。共享测试整理另完成授权/迁移/投递组 292 通过 / 7 跳过、原生后半与执行组件组 473 通过 / 14 跳过。这些 SQLite 运行只作开发快测补证，运行服务依然使用 PostgreSQL。

## 4. 容量结果应该怎样使用

该基线使用单机实际 PostgreSQL TCP、16 个合成人员/独立会话/工作项，测 production service 方法；混合曲线覆盖 UserInfo、refresh、工作项查询和编辑各 25%，另单独验证完整密码＋TOTP→PKCE code/token 的 16 人共享出口突发。它不包含 HTTP 网关、真实客户端、WAN、多进程和生产数据规模。

guard 获取时间包含驱动与 SQL 往返，是服务端锁等待的上界；持有时间包含事务结束后的少量连接归还开销。SQL 数不包含驱动事务控制/pre-ping。各操作吞吐是其在同一混合窗口的贡献，不能当成单操作容量极限。

从曲线判断，当前组织级协调在提高并发时会增加排队；保留既有锁是本轮有依据的决定。下一轮优化应先按真实目标分解 SQL 和锁域，再补撤权/提交竞争证明；不能直接用 Redis 或缓存替代当前事实检查。生产 SLO 和入口代理验收继续属于实际部署工作。

## 5. 当前结构与交付边界

更新后的[全景/目录](../stage-architecture-panorama.md)、[完整符号地图](../stage-code-map.md)和[机器清单](stage-architecture-inventory-2026-10-26.json)对应整改后的源码：107 个 app Python 文件、244 个类、622 个函数/方法定义，25 个 HTTP 操作、37 张表，迁移 head 仍为 `c6f3a19d820b`。本轮没有增加业务权限、HTTP 路径或物理表。

冻结 manifest 绑定源码、依赖、迁移和制品；验证记录绑定实际检查输入及结果。制品创建本身不等于测试通过。生产级身份运营、实际 Maestro 联调、Passkey、持久工作流、多节点和通用配置蓝图仍按各自路线验收。
