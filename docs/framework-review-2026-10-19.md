---
title: Stage 基础框架审查与修复记录
date: 2026-10-19
status: review-record
classification: internal
policy_effect: none
owner_role: Stage 工程维护岗位（待指定）
---

# Stage 基础框架审查与修复记录

本轮检查当前实际代码，并行审阅授权、存储/迁移及运行边界。目标是让底板有明确且可验证的基础行为，不扩展完整 OA 业务或动态插件平台。

## 已确认并修复的问题

| 问题 | 修复方式 | 验证 |
|---|---|---|
| 写授权可以省略预期版本，资源状态规则由调用者提供 | 中央动作策略控制状态；写动作同时要求正整数当前/预期版本 | 版本缺失、过时、非法类型和越阶段执行回归测试 |
| Agent 可以拼接自身关系与委托人角色扩大权限 | 委托只能收窄委托人的权限；关系基于被代理主体核验 | 自身关系不能补齐委托人缺少关系的反例 |
| 撤销、停用、尚未生效统报过期，策略版本只作为字符串装饰 | 独立失败原因；显式内置策略 ID，未知版本拒绝；决策 ID 关联请求和任职 | 时间窗口、策略 ID、关系确定性及只读注册表测试 |
| MySQL/SQLite 时间可能变成 naive，与授权内核不一致 | UTCDateTime 拒绝 naive 写入、归一化 UTC 读取；数据库默认时间与会话 UTC | SQLite 实际往返及三方言类型/DDL测试；真库测试入口 |
| 标识符比较依赖各库默认排序规则 | ExactString 使用 MySQL NO PAD binary、PostgreSQL C、SQLite BINARY | 大小写、重音、尾随空格、唯一约束测试 |
| 迁移没有旧数据与格式变化保护，编码密码可能触发 ConfigParser 错误 | 保留 initial，增加前向预检迁移；旧 naive 时间需显式确认；URL 百分号转义 | 升级/漂移检查/降级/再升级；旧数据拒绝、显式确认与保留 |
| 连接池未接入关闭，session 提交责任不清楚 | app lifespan 管理 engine；命令负责显式事务，请求依赖不自动 commit | 正常/异常关闭释放、未提交回滚、失败回滚和显式提交测试 |
| 配置 repr/错误可能回显密码，生产可误用演示配置 | 隐藏 URL 与输入文本；检查 URL 结构；生产要求显式生产库配置 | 虚构凭据不回显、生产默认/SQLite拒绝测试 |
| HTTP 错误没有统一关联 ID，通用异常重抛可能被服务器记录原始内容 | 请求边界处理未开始响应的异常；已开始响应中断并屏蔽原异常；保留标准协议头 | 默认 ASGI transport、日志脱敏、取消/断连/流异常、405/401/429协议测试 |
| CI 只测纯逻辑与 URL 白名单 | 新增 MySQL 8.4 / PostgreSQL 17 临时服务门禁 | 实际执行结果以未来 GitLab pipeline 为准，不能视为本地已验收 |

## 存储适配范围

MySQL/PostgreSQL 的基础连接、会话、UTC 时间、精确字符串、模型约束与迁移形成一套小型适配层。业务模块继续使用统一对象和命令，不分叉权限逻辑。

MySQL 与 PostgreSQL 会话使用 READ COMMITTED；这不是业务并发安全证明。未来任务领取仍需版本条件更新、唯一约束、正确事务边界和实际竞争测试。SQLite 不作为并发测试替代品。

新迁移不猜测旧 MySQL/SQLite 时间的来源，已有数据需先确认或校正为 UTC。由于回退比较规则或时间精度可能丢失信息，向下迁移只允许空 Stage 表；不对有数据环境自动“回滚”。详见[迁移说明](../migrations/README.md)。

## 双生产库验收入口

GitLab CI 的 `database:mysql` 与 `database:postgresql` 在独立临时服务中执行：

```text
upgrade head → check → downgrade base → upgrade head → check
→ 时间/标识符/约束存储测试
```

本地如有专用空测试库，可设置 `TEST_DATABASE_URL`，数据库名称需以 `stage_test` 或 `stage_ci` 为前缀，并显式设置 `STAGE_ALLOW_TEST_DATABASE_RESET=1`，再运行：

```bash
uv run python scripts/check_database.py
uv run pytest -q tests/storage
```

脚本拒绝非空库；仅允许显式授权的测试目标。存储语义测试使用随机 `stage_probe_` 表，不删除已有业务表。不要把上述脚本用于业务库或生产库。

## 本轮验证与限制

本地验收结果：98 项测试通过，Ruff 检查与格式检查、Mypy、OpenAPI 完整结构比较和 `uv lock --check` 均通过。额外验证了受保护测试库脚本的完整 SQLite 迁移循环，以及独立临时虚拟环境中不含开发依赖的锁定安装和 MySQL/PG 驱动、应用及模型导入。

本地验证纯授权边界、SQLite 迁移/存储往返、运行生命周期、错误响应、配置和 OpenAPI 契约，以及三方言 DDL 编译。当前机器没有 MySQL/PostgreSQL 服务与 Docker，因此双库服务测试、容器构建和远端 CI 尚未实跑。

已实现的是基础骨架，不包含身份认证、权威授权上下文加载、完整审批、事务幂等存储、审计/outbox 持久化、投递 worker、Redis 运行态、GitLab 连接器或 OA 工作流。当前公开业务端点只有 liveness。

这些能力不必全部在本轮补齐。接第一个业务写 API 前，必须先完成认证及上下文加载、组织范围过滤、命令事务、幂等唯一键、审计和 outbox 原子写入；不能把现有 dataclass 当成这些保证已经存在。

## 来源与复核

来源为本地 Stage 代码、回归测试及本轮独立审查；这是工程记录，不代表企业治理批准。引入业务写 API、调整数据库版本/比较规则、扩展策略版本或改变运行边界时复核，具体维护与审阅岗位待指定。
