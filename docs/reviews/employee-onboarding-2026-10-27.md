# 员工开户与一次性激活：实现与审计

本轮在 `feat/employee-activation` 延续人员管理路线，实现“获权管理员开户 → 本人设置密码与 TOTP → 正常 OAuth 登录”的服务端闭环。业务任职仍由现有有限授予入口单独分配。规格与 Maestro 接缝见[开户合同](../employee-onboarding.md)，管理能力启用见[运行手册](../../deployment/identity/onboarding-capabilities.md)。

## 架构结论

保持组件化模块单体。`app.administration.onboarding` 管理人员创建、证明重发／撤销、历史结果及审计；`app.native_auth.activation` 管理浏览器凭据激活；`app.native_auth.onboarding_source` 固定原生 profile、来源、MFA 和客户端准入。复用现有 identity/storage/native-auth provider 与调用租约，不增加 Runtime Core 概念、服务进程、消息队列或通用工作流。原生认证关闭时现有管理可用，开户失败关闭；即便 native provider 意外存在也不能绕过配置开关。

新管理角色 revision 2 是精确固定的七项动作合同，原 revision 1 的四项及摘要保持不变。必须显式版本化升级已接受的首管任职；数据库迁移不授予权限。开户只建立新的 suspended 人员／成员／映射及 pending 账号，不能提前登录或获取任职。所有安全变更使用短组织 X 事务；Argon2 在事务外，公开状态访问也计入 peer 请求预算。普通业务协调与 Core 保持现有边界，不以本轮证明生产吞吐能力。

迁移 `d8e4b6c2a901` 新增一张激活表，总计39张应用表。active 账号必须保留密码哈希；取消账号可以无密码且保留身份／用户名预约。已有账户及关联记录被保留，有任何激活事实时拒绝删除性降级。新增6个管理操作及4个 issuer 操作，总计44个HTTP操作；原34个路径与 operationId 未改变。

## 深度复查及修正

- 旧角色默认扩权风险：分别固定 v1/v2，通过当前角色 revision 判定动作，升级具有双版本与 OS 审计。
- 认证来源／人员改变：每阶段固定版本与配置摘要，失效时清理未完成证明，不能在来源恢复后继续使用旧 ceremony。
- 秘密重放：管理 journal 仅存安全元数据；首次提交响应丢失后必须查原键，需要码则明确重发。恢复码也只交付一次，重试及状态查询不返回旧明文。
- 过期与竞争：原邀请和浏览器五分钟期限均为绝对期限；TOTP 错误预算耗尽须重发。完成与重发／撤销取得同一目标 Principal 和组织保护，避免先后次序混合。
- 浏览器恢复：未知结果响应续保同一专用 cookie，保留状态查询所需证明，但不延长数据库有效期；完成后的安全结果也只能在原窗口内恢复。
- 公开查询压力：状态查询在进入排他事务前执行 peer 限流，错误浏览器不能借公开引用无预算竞争组织锁。
- 模块装配：管理新路由使用独立 APIRouter 显式组合，避免 import 副作用污染既有 router；配置关闭与 provider 存在分别测试。
- 真实浏览器：保留同源 Origin／CSRF、严格表单及 cookie 唯一性，激活码不进入 URL；创建应用的真实 provider 与迁移数据库共同接受 Chromium 验收。

## 验证记录

最终计数与源码摘要记录在[验证 JSON](employee-onboarding-validation-2026-10-27.json)；原始 JUnit 留在本机临时目录。SQLite 仅作快测，PostgreSQL 17 是权威并发／持久化验收环境，Chromium 使用独立可重建测试数据库；未改动共享开发实例的人员或凭据。

| 检查 | 结果 |
|---|---|
| PostgreSQL 17 全量回归 | 1,866 通过、11 按环境跳过；跳过的3个浏览器场景由独立门禁执行 |
| PostgreSQL 开户并发专项 | 7 通过，亦包含在全量回归中 |
| SQLite 快测基线 | 1,755 通过、111 跳过；不作为生产数据库兼容承诺 |
| 最终管理／激活 HTTP 补充回归 | 54 通过，覆盖快测基线收集后的补充用例及最后修正 |
| Chromium 强制门禁 | 3/3 通过，使用最终实现、真实 provider 及迁移 PostgreSQL |
| Runtime conformance | 46/46 通过 |
| PostgreSQL 迁移 | upgrade / check / downgrade base / upgrade / check 全部通过 |
| Ruff / 格式 / Mypy / OpenAPI | 全通过；303个格式文件、299个类型检查源码 |
| Wheel | 独立导入及迁移 head 检查通过，140个包内源码与工作区逐字节一致 |
| 文档 | 本地链接、锚点、代码块及架构 Mermaid 与源图同步通过 |

当前未选择外部 Keycloak 实例和共享在线探针，不将这些默认跳过项计为通过。原生密码／TOTP、OAuth 登录、无自动任职与浏览器边界都有本轮实际证据。

## 保留边界与下一步

本轮尚不提供人员姓名／HR 主档、离职组合冻结、再入职、多管理员、管理员恢复 HTTP、邮件发送、Passkey 或 Maestro 页面。激活码采用管理员核实后安全人工交付；密码和 TOTP 留在 issuer。配置升级脚本仍限定 dev/test，生产启用和运营恢复需独立交付。

管理 `issued/enrolling` 是记录状态，不保证五分钟 ceremony 仍有效；过期秘密在有效流程访问或重发／撤销时清理，后台清理尚未加入。该限制及重发方式已写入客户端合同。下一步优先在 Maestro 对接已固定的管理合同，再独立设计人员安全恢复／离职的组合状态与会话撤销边界，避免混入本次开户协议。
