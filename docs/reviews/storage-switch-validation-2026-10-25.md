---
title: PostgreSQL 与 Redis 存储切换验证
status: validation-record
review_date: 2026-10-25
policy_effect: none
baseline_commit: eb11e4371ce212629ba9dee3bbe811ebd006ca48
---

# PostgreSQL 与 Redis 存储切换验证

本轮落实用户确定的 PostgreSQL＋Redis 长期组合，工程基线为 PostgreSQL 17 与 Redis 8。配置默认改为 PostgreSQL，移除 aiomysql/pymysql 运行依赖，拒绝 MySQL URL；保留 SQLite 快测及历史迁移方言。Redis 提供显式配置和异步客户端工厂，尚未接入 Core 或业务缓存。

验证针对上述提交加本轮工作区改动；原有未提交代码审查文档继续保留。测试使用虚构数据和隔离临时服务，没有迁移真实企业数据库。服务以非 root 身份运行，只监听回环地址，验证后关闭；开发环境由仓库 `compose.yaml` 提供。

## 实际环境与结果

| 检查 | 环境与结果 |
|---|---|
| Python / 依赖 | Ubuntu 24.04 amd64，Python 3.12.3；使用 uv 0.13.1 执行 `uv sync --locked --dev` 与 `uv lock --check`，通过 |
| 默认完整测试 | `.venv/bin/python -m pytest -q --tb=short -p no:cacheprovider`，**228 passed，35.90s**；使用临时 SQLite |
| PostgreSQL 完整测试 | 同一命令，显式 `TEST_DATABASE_URL` 与测试重置授权，**236 passed，33.57s**；执行/投递使用真 PostgreSQL，存储探针增加 8 个 PG 参数用例，其他测试保留其原环境 |
| PostgreSQL 迁移 | PostgreSQL **17.11**：`scripts/check_database.py` 的 upgrade head → check → downgrade base → upgrade head → check 全部通过 |
| 存储探针隔离 | 在已经存在 Stage 表的 PG 测试库运行 `tests/storage/test_portable_types.py`，**26 passed**，包括 SQLite 与 PG 参数集 |
| Redis | Redis **8.2.3**：`scripts/check_redis.py` 通过 PING、随机 key 的 SET NX、值与 TTL、实际过期和异步关闭 |
| 静态与类型 | Ruff 检查/格式、Mypy（71 个源文件）通过 |
| HTTP 契约 | `STAGE_ENV=test .venv/bin/python scripts/check_contract.py --check` 通过，外部 API 未变化 |
| Compose | 官方 Compose **2.39.2** 执行 `config --quiet` 通过，确认开发端口仅绑定 127.0.0.1:15432 / 16379 |
| 文档与配置 | 修改文档相对链接、Markdown 围栏、YAML 解析和 `git diff --check` 通过 |

PG 服务来自验证签名与包摘要的 PGDG 官方软件包，Redis 来自按官方 hashes 验证的源码。安装/编译仅在任务临时目录进行，没有安装全局服务或更改 systemd。Compose 采用 `postgres:17` / `redis:8` 大版本标签，上述实际测试版本是此次证据，不等于锁定了容器镜像摘要。

## 真库测试发现并修复的问题

原存储探针只给临时表和普通索引改名，保留了 `uk_role_assignment` 等唯一约束名。PostgreSQL 会为唯一约束创建 schema 范围内命名的索引，与迁移建立的正式表冲突。另外，`to_metadata()` 已将新表名带入自动索引名，原逻辑再次添加前缀，使名称超过 PostgreSQL 的 63 字节限制。

现在探针的命名约束、索引均使用独立测试前缀，并采用 SQLAlchemy 的方言截断规则；不修改生产表、既有 Alembic revision 或业务约束。上表存储专项与完整 PG 回归验证了修正结果。

本轮同时检查了 Redis 开发健康探针：`redis-cli` 增加 `-e`，认证/命令错误会返回失败退出码，避免输出 NOAUTH 但误报健康。Redis URL 不能通过查询参数覆盖超时/TLS，rediss 强制证书及主机名验证；客户端关闭自动命令重试，未知写入结果交由调用方处理。配置验证错误的 JSON/结构化输出也去除了秘密输入。

## 验证边界

本次没有运行 Docker daemon、构建 Stage 容器或执行远端 GitHub/GitLab 流水线；Compose 已验证的是配置解析。已新增 GitHub Actions，并同步 GitLab 模板中的 PostgreSQL/Redis 门禁。

Redis 尚无正式业务使用方，当前探针和客户端测试不能代表缓存撤权、跨租户缓存隔离或故障降级已验收。Runtime Core、真实身份认证、签名准入、WASM 和资格评估仍按 P1 规格建设；原审查中的投递 R1/R2 及任务授权缺口不属于本轮存储切换修复范围。
