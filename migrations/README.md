# Stage migrations

这里保存 Stage 数据库迁移。第一版底板包含基础身份、角色任职、关系元组和工作项表；它们不代表企业已经配置真实组织。

```bash
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic upgrade head
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic downgrade base
```

企业使用 MySQL 时设置对应的 `STAGE_DATABASE_URL`，迁移只在目标测试库验证后进入部署。MySQL 验证基线为
8.4 LTS，使用 `utf8mb4_0900_bin`（NO PAD）使标识符区分大小写、重音和尾随空格；不宣称兼容 MariaDB。

PostgreSQL 使用：

```bash
STAGE_DATABASE_URL=postgresql+asyncpg://stage:stage@127.0.0.1:5432/stage uv run alembic upgrade head
```

## 时间与标识符

应用层写入的时间必须带时区。读取统一返回 UTC aware `datetime`。PostgreSQL 保存为 `timestamptz`，MySQL
保存为 UTC `DATETIME(6)`，SQLite 保存为 UTC 无时区值。数据库默认时间也明确按 UTC 生成，不依赖会话时区。
标识符、角色/关系代码、状态使用列级精确排序规则：MySQL `utf8mb4_0900_bin`、PostgreSQL `C`、SQLite `BINARY`。
面向用户的标题和描述保留普通字符串语义。角色、关系代码、状态、工作生命周期和数据分类只做非空与结构约束，
不在数据库中冻结企业枚举；核心主体类型仍由当前运行时契约固定。

`870845fbe1f0` 是保留不变的首次迁移；`6d9e0b2f4a71` 是前向兼容修正。新迁移在任何 DDL 前检查旧数据是否
满足状态、版本和时间区间约束；发现异常时拒绝变更，先由维护者修正。已有数据的 MySQL/SQLite 缺少旧时间的
时区来源，因此默认拒绝升级。维护者必须确认或先将所有旧时间（含 created_at/updated_at）校正为 UTC 后，显式执行：

```bash
uv run alembic -x legacy_timestamps=utc upgrade head
```

该参数是对旧数据来源的确认，不执行时区转换，也不能恢复历史精度。空库无需参数；首次迁移中的 PostgreSQL 时间
已经是 `timestamptz`，无需做此假定。迁移需要在线连接，以完成数据预检，不支持 `--sql` 离线生成。

向下迁移移除精确比较和微秒精度时可能丢失语义，因此只允许空的 Stage 表。已有数据的恢复应使用经过验证的备份。
MySQL DDL 本身不能像 PostgreSQL DDL 一样整体事务回滚，升级前应备份并安排停写；预检不代替备份。

## 验证范围

`tests/storage` 默认使用临时 SQLite 库验证时间往返、唯一键、约束、迁移循环与模型漂移，并编译三种方言的 DDL。
设置 `TEST_DATABASE_URL` 后，同一组存储语义测试还会运行于指定的 MySQL/PostgreSQL 实例。测试只创建和删除带随机
`stage_probe_` 前缀的专属表，不删除数据库、schema 或已有业务表；仍应只传入专门的测试库。真实两库的迁移循环由 CI
在各自的临时测试数据库验证，SQLite 测试不替代真实并发或锁行为验收。
