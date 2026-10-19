# Stage migrations

这里保存 Stage 数据库迁移。第一版底板包含基础身份、角色任职、关系元组和工作项表；它们不代表企业已经配置真实组织。

```bash
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic upgrade head
STAGE_DATABASE_URL=sqlite+aiosqlite:///./stage.db uv run alembic downgrade base
```

企业使用 MySQL 时设置对应的 `STAGE_DATABASE_URL`，迁移只在目标测试库验证后进入部署。
