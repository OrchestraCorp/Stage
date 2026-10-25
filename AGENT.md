# Stage Runtime Repository Instructions

本仓库是 OrchestraCorp Stage 的运行服务底板，不是 ScoreBook 的制度仓，也不保存真实企业治理正文。

## 权威边界

- ScoreBook 保存批准的规则、岗位契约、正式知识和发布版本。
- Stage 保存当前组织关系、任务、承诺、授权、运行、审计和实际加载记录。
- GitLab 保存实际仓库成员、可见性和分支权限；Stage 只保存期望、请求、核验和漂移状态。
- 专业业务系统保存自己的领域事实；Stage 保存必要引用与协作状态。
- Virtuoso 的私人会话和个人缓存不自动进入 Stage。

## 开发规则

1. 业务代码只调用 `app.authz` 的能力判定，不直接判断角色名称。
2. 权限、关系、属性、对象版本和业务事务分别测试；不能用一条端到端测试代替边界测试。
3. 所有写命令必须定义幂等键、对象版本、审计和失败/未知结果。
4. 不让 Agent、连接器或配置蓝图直接写数据库；通过领域命令、outbox 和受控接口执行。
5. 不把本地测试配置、凭据或真实企业数据提交到仓库。
6. 修改 API 时同步更新 `contracts/openapi.yaml` 和契约测试。
7. PostgreSQL 17 是唯一长期主库，Redis 8 仅作可重建缓存、非权威短期协调和可丢失唤醒；具体使用须定义故障策略。授权、幂等、outbox 与业务租约权威留在 PostgreSQL，不承诺 PostgreSQL/Redis 双写原子性。
8. Redis 清空不能丢业务事实；缓存须隔离 tenant 并核验当前授权版本，不能绕过撤权。SQLite 仅快测，MySQL 已退出受支持配置；历史迁移与验证记录保留。存储细则见 [存储策略](docs/storage-strategy.md)。

## 当前状态

P1a 已实现并在云开发设备验证：独立 Runtime Core、PG/Redis provider、可信注入的命令/投递组件及 `/health`、`/ready`。RBAC/ReBAC/ABAC 仍为纯判定，真实身份 loader、WASM、签名准入与业务 HTTP 尚待后续阶段。验收范围见 [P1a 记录](docs/reviews/p1a-validation-2026-10-25.md)，不据此声明真实企业制度生效或生产验收完成。
