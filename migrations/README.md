# Stage migrations

这里保存 Stage 数据库迁移。第一版底板包含基础身份、角色任职、关系元组和工作项表；它们不代表企业已经配置真实组织。

```bash
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic upgrade head
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic downgrade base
```

2026-10-25 起，PostgreSQL 17 是唯一长期主库；Redis 8 不承担 Alembic 业务 schema。MySQL 退出当前支持范围，历史 revision 及其中的 MySQL 分支保留，不继续据此承诺生产兼容。SQLite 只用于适用子集的快速检查。完整边界见[存储策略](../docs/storage-strategy.md)。

开发 compose 的 PostgreSQL 使用：

```bash
STAGE_DATABASE_URL=postgresql+asyncpg://stage:stage-dev-postgres@127.0.0.1:15432/stage uv run alembic upgrade head
```

## 时间与标识符

应用层写入的时间必须带时区。读取统一返回 UTC aware `datetime`。PostgreSQL 保存为 `timestamptz`，SQLite 快测保存为 UTC 无时区值。数据库默认时间明确按 UTC 生成，不依赖会话时区。
标识符、角色/关系代码、状态使用列级精确排序规则：PostgreSQL `C`、SQLite `BINARY`。
面向用户的标题和描述保留普通字符串语义。角色、关系代码、状态、工作生命周期和数据分类只做非空与结构约束，
不在数据库中冻结企业枚举；核心主体类型仍由当前运行时契约固定。

`870845fbe1f0` 是保留不变的首次迁移；`6d9e0b2f4a71` 是前向兼容修正。新迁移在任何 DDL 前检查旧数据是否
满足状态、版本和时间区间约束；发现异常时拒绝变更，先由维护者修正。已有数据的 SQLite，以及历史 MySQL 分支，缺少旧时间的时区来源，因此默认拒绝该修正。维护者必须确认或先将所有旧时间（含 created_at/updated_at）校正为 UTC 后，显式执行：

```bash
uv run alembic -x legacy_timestamps=utc upgrade head
```

该参数是对旧数据来源的确认，不执行时区转换，也不能恢复历史精度。空库无需参数；首次迁移中的 PostgreSQL 时间
已经是 `timestamptz`，无需做此假定。迁移需要在线连接，以完成数据预检，不支持 `--sql` 离线生成。

向下迁移移除精确比较和微秒精度时可能丢失语义，因此只允许空的 Stage 表。已有数据的恢复应使用经过验证的备份。
升级前应备份并安排必要的写入管控；预检不代替备份。历史 MySQL DDL 的非事务行为继续由原迁移处理，不改写已发布 revision；本轮不执行旧库跨引擎数据搬迁。

## 验证范围

`tests/storage` 的临时 SQLite 测试承担时间往返、唯一键、约束、迁移循环与模型漂移的适用子集。正式目标为 PostgreSQL 17；新增约束和锁行为不再受 MySQL 兼容要求限制，须提供 PostgreSQL 真库证据。

设置 `TEST_DATABASE_URL` 时只传入专门测试库。存储语义探针只创建和删除随机 `stage_probe_` 前缀表；迁移循环和执行测试有各自的重置授权要求，不能据此前缀保证推断整套测试不会重置已有表。CI 目标改为 PostgreSQL 17，SQLite 不替代真实并发、锁或迁移验收。本轮 PG17.11 迁移循环及测试结果见[存储切换验证](../docs/reviews/storage-switch-validation-2026-10-25.md)，历史双库验证记录不改写。
