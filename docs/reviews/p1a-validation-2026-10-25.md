---
title: P1a 框架实施与云设备验收
status: validation-record
review_date: 2026-10-25
policy_effect: none
baseline_commit: eb11e4371ce212629ba9dee3bbe811ebd006ca48
---

# P1a 框架实施与云设备验收

P1a.1 资源与依赖内核、P1a.2 安全退出与组合、P1a.3 应用装配已实现并在本机验收。基线是上述提交加当前工作区；存储切换和设计文档的既有改动保留。本记录不将 P1a 完成扩大为真实 IAM、WASM、签名准入、完整任务业务或生产可用性保证。

## 实际交付

| 范围 | 交付 |
|---|---|
| Core | 纯标准库 RuntimeHost、PluginSpec、ComponentContext、effect ownership、响应式依赖、committed/inherited pins、generation/revision/registration、scope/inject、任务监督和结构化报告 |
| 关闭/事件 | 依赖闭包原子停接入、全 owner stop、调用与事件 drain、旧依赖收尾、独立清理与 blocked 保护、共享 close、等待环诊断、五种事件模式 |
| Providers | 有限 PG/Redis 连接预算、连接/schema 探测、故障与恢复通知、独立 session factory；业务事务继续由 CommandKernel 管理 |
| 应用装配 | FastAPI lifespan 管宿主；`/health` 保持存活语义，`/ready` 返回 200/503；必需/可选组件明确，关闭失败保留 runtime 供诊断与再次等待 |
| 命令/投递 | 可信注入的 command/delivery plugins，Core 租约与 worker 任务监督；真实身份尚未提供，默认 HTTP 不开放 fixture 业务入口 |
| 投递修复 | R1 非字符串回执转 unknown；R2 poll 故障立即传播/可观察，停止新接入，不自动盲重发 |

独立审查发现并修正了旧 context.scope 穿代登记、child 读取 LOADING 祖先服务、cleanup 错误聚合丢项、跨 realm 等待环误判，以及应用装配必需组件遗漏和失败关闭后的诊断入口丢失。各项有回归测试。

## Python 与真实服务验证

| 检查 | 结果 |
|---|---|
| 默认完整测试 | **314 passed，7 skipped**；跳过项是未显式配置的真实服务/连接池冒烟目标 |
| PostgreSQL＋Redis 完整测试 | **324 passed，35.02s**，5 项 smoke 目标测试跳过；PG 17.11、Redis 8.2.3，独立 `stage_test_final_b48ef268` 库，未重置 `stage` 开发库 |
| Core 场景映射 | **34 C＋12 S＝46/46**；所有映射到的 pytest nodeid 在真实服务全量 JUnit 中成功，结果见[机器记录](p1a-runtime-conformance-2026-10-25.json) |
| 迁移 | 专用 PG 测试库 upgrade/check/downgrade/upgrade/check 通过；应用启动只检查版本，不执行迁移 |
| Redis | PING、随机 key NX/TTL/实际过期、池上限耗尽与恢复、有限读取超时通过 |
| 契约与质量 | OpenAPI 包含 `/ready` 并与实现一致；Ruff、格式、Mypy、文档与差异检查以本轮最终命令记录为准 |
| Live smoke | 云设备 `/health`、`/ready`、OpenAPI、并发请求、PG `SELECT 1`、Redis `PING` **5 passed**；未配置目标时 5 项安全跳过 |

场景映射不是独立测试实现；`scripts/check_runtime_conformance.py` 必须解析真实 JUnit，缺项、失败、跳过均拒绝。CI 的真实服务 job 已运行全套测试并调用此门禁。代码摘要记录在机器证据中，可识别测试后修改；远端 CI 本轮未执行。

## 固定 Cordis 参考

参考固定为 Maestro `6f7124baabca582d43ce84fd59d2f48e4b790763`，由 git archive 提取 Cordis 与同提交 cosmokit，在隔离目录中以 Node 25 原生 TS 转换执行。没有安装或修改 Maestro 工作区依赖。

参考执行、共享操作的 TS/Python 双侧比较以及 D-01～D-09 的差异观测见 [reference manifest](runtime-reference-2026-10-25/manifest.json) 和 [runner 说明](../../scripts/runtime_reference/README.md)。参考侧按 C 编号执行的片段不冒充完整 Stage 场景；完整 Python 义务由上述 46 场景门禁独立验证。论文没有在本轮重新运行或重新读取，且不存在本轮声称的官方 Cordis 认证。

## 云设备运行

三个 systemd 服务以 `orchstage` 非 root 用户运行，并已启用开机启动/失败重启：Stage `127.0.0.1:8080`，PostgreSQL `127.0.0.1:15432`，Redis `127.0.0.1:16379`。配置与设备专用密码在 root-only 环境文件，未入库；应用源码只读绑定到服务命名空间。

本轮实际停止/恢复两个依赖并验证：

- Redis 停止：`/ready` 仍为 200，Redis 状态为 unavailable；`/health` 200，恢复后自动就绪。
- PG 停止：`/ready` 503、`/health` 200；PG 恢复后自动恢复 200。
- HTTP **500/500 成功，25 并发**，范围为系统探针，不代表业务吞吐基准。
- Stage 连续 **3 次受控重启**后均恢复就绪；故障注入最终全部恢复，服务保持运行。

实时返回、测试时 Core 摘要及 systemd 状态见[设备验证记录](p1a-device-validation-2026-10-25.json)。启动、日志、迁移及 SSH 访问方式见[开发设备运行说明](../../deployment/dev/README.md)。该验证是有界功能与故障测试，尚未执行长期浸泡、生产容量或多节点可用性验收。

后续继续 P1b 真实认证和权威身份加载、P1c WASM/签名准入、P1d 资格评估。现有短事务 kernel 仍不能直接在 handler 中执行 WASM/网络；真实 IAM 的全局主体/信任锁顺序也须按原方案扩展。
