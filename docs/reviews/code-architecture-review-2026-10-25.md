---
title: Stage 代码架构复查与冒烟测试
status: review-record
review_date: 2026-10-25
policy_effect: none
---

# Stage 代码架构复查与冒烟测试

本轮复查围绕 P1a 实现后的 Runtime Core、provider、命令/投递装配和云设备运行状态进行。复查没有发现仍需阻断交付的架构问题；期间发现并修正了三个会影响稳定性的边界：

- `ApplicationRuntime.start()` 增加启动锁和幂等保护，重复启动不会留下重复 provider 或失败组件。
- 命令服务改为每次操作重新取得 PostgreSQL Core lease，释放外层服务引用后，进行中的命令仍保持 storage pin，关闭会等待事务收敛。
- 投递 worker 在数据库事务阶段采用 cooperative stop，在外部 sink I/O 阶段才取消；active operation 显式纳入关闭等待，避免 PostgreSQL cleanup 被取消污染成 `ExecutionCleanupFailed`。

复查确认以下边界保持成立：Core 不导入 IAM/ORM/FastAPI；PostgreSQL 保存租约、幂等、审计和 outbox；Redis 只作辅助状态；`/health` 只报告存活；`/ready` 根据受管 provider 反映依赖状态；WASM 尚未接入命令事务。

## 冒烟测试

新增的[只读冒烟套件](../../tests/smoke/README.md)默认跳过，只有显式设置目标才访问服务。它覆盖：

- `/health`、`/ready` 返回结构和状态码；
- OpenAPI 存在性与系统探针描述；
- 读探针并发、唯一 request ID 和响应脱敏；
- PostgreSQL `SELECT 1`；
- Redis `PING`。

它不重置数据库、不写 Redis key、不执行 `FLUSH*`。当前云设备加载 `/etc/orchestra-stage-dev.env` 后运行 `scripts/smoke.py`，结果为 **5 passed**；未设置目标时为 **5 skipped**。

专项稳定性验证还完成了：

- P1a 真实 PostgreSQL/Redis 全量测试：**324 passed，5 skipped**；跳过项是未配置 live smoke 目标；
- SQLite 全量测试：**314 passed，7 skipped**；
- delivery component close 重复运行 **20 次通过**；worker cancellation close 重复运行 **10 次通过**；
- Core/生命周期/命令/投递重点子集：**127 passed**；
- Core 场景映射仍为 **34 C＋12 S = 46/46**；
- 云设备 3 个 systemd 单元 `active`、`NRestarts=0`、`ExecMainStatus=0`；当前 `/health` 和 `/ready` 均为 200；此前 500 请求、25 并发及 PG/Redis 停止恢复探针均通过。

设备配置仍有一个运维提示：Redis 启动报告 `vm.overcommit_memory=0`。这不影响本轮功能测试，但生产或长期运行前应由设备运维设置为 `1`，并重新观察 Redis 的后台保存/复制行为。

## 当前边界

P1a 已完成 Core、生命周期和基础 provider 装配。真实身份 loader/ServiceGrant、WASM runner、签名准入、业务命令 HTTP 接口和生产 sink 仍按 P1b～P1d 建设。冒烟套件证明运行实例的系统边界可观察，不能替代这些业务阶段验收。
