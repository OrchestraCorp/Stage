# 共享测试支持代码

这里保存多个测试或验收脚本共同使用的合成数据、fixture、构造器和数据库工具。
测试断言与场景仍留在各自 `test_*.py`；支持包不能反向导入测试模块，也不包含待收集测试。
`tests/test_support_boundaries.py` 用 AST 检查这条边界。

| 支持模块 | 内容 |
| --- | --- |
| `authorization`、`database_commands`、`work_details` | 当前授权事实、工作项与命令种子 |
| `command_kernel`、`authenticated_commands`、`authorization_recovery` | 可信内核 fixture、真实身份组合、结果读取授权 |
| `identity_tokens`、`identity_service`、`identity_administration`、`identity_assurance` | 签名令牌、人员映射与受控策略配置 |
| `assignment_http`、`work_http` | 任职 HTTP fixture 与私密响应断言 |
| `native_*` | 原生认证、OAuth、浏览器、会话、凭据和管理数据 |
| `delivery` | 投递时钟、接收端和持久消息构造器 |
| `postgres`、`human_concurrency` | 真实数据库阻塞边、任务清理、人员命令竞争准备 |
| `migrations`、`live_database` | SQLite 迁移和显式授权的 PostgreSQL 迁移 fixture |
| `database_safety` | 共享数据库并行重置保护 |

pytest 通过当前测试模块可见的 fixture 名称解析依赖。测试按需显式导入 fixture，
例如 `from tests.support.native_oauth import oauth as oauth, native_keyring as native_keyring`。
不要仅导入依赖这些 fixture 的普通函数；不要为方便而注册全部支持模块为全局插件。
`as` 同名重导出用于保留 fixture 可见性，避免未使用导入清理移除它。

配置 `TEST_DATABASE_URL` 时，一次 pytest 进程独占该临时数据库，并运行 `-n 0`。
`pytest_configure` 在收集和连接数据库前拒绝 xdist controller/worker 对共享库的使用。
未配置该变量时，SQLite 使用每个测试自己的 `tmp_path`，可独立运行。
该保护不会协调两个分别启动的 pytest 进程；PostgreSQL 验收仍必须由操作者串行执行，
或为每个独立进程显式分配不同的获准测试数据库。

`scripts/benchmark_human_work.py` 直接复用本包，并将依赖的支持文件纳入输入指纹，
避免把测试文件当运行库或在基准中维护另一份授权种子。
