# 架构图

[当前代码架构与 Cordis 对照](../current-code-architecture.md) · [当前代码 Mermaid 源](stage-current-code.mmd)：默认装配身份、本人任职发现和人员工作项服务，通用命令与投递仍需显式注入；身份及工作项路由统一 `/api/v1`，组织根由服务端固定，部门/项目使用 scope。

[主架构 PNG](stage-architecture.png) · [SVG 矢量版](stage-architecture.svg) · [Mermaid 可编辑源](stage-architecture.mmd)

图对应[基座架构](../architecture.md)、[一期方案](../phase-1.md)、[核心对象](../core-object-contracts.md)及[长期路线](../roadmap.md)。SVG/PNG 是一期完整目标图；当前代码由 `stage-current-code.mmd` 与文档内联图描述。P1b.3 已接通真实人员证据到最小元数据命令、提交前重验和原 key 恢复，见[本轮记录](../reviews/p1b3-human-work-2026-10-25.md)。P1b.4 已实现本人任职发现和密码＋TOTP 的组织统一 MFA 策略，真实隔离 IdP 与 Stage 路径已有证据；本轮原生模块已实现服务端因素绑定/恢复，实际 Maestro 桌面与生产恢复流程仍待验收；业务批准 profile 未冻结，WASM 与签名准入继续后续阶段，目标图不代替实际部署证据。

2026-10-25 按[人员—公司—人员](../people-company-collaboration.md)对齐入口与责任：Agent 是人员监督下的辅助工具，没有独立业务入口或任职；worker 是受管技术执行设施，保存原人员命令的因果链，业务继续执行与外发按协议核验当前权限。技术凭据和生成内容都不等于人员确认。

## 图例与执行边界

- 蓝色：一期可信Stage宿主，包括自研Core、身份、准入、输入准备、命令与事务提交。
- 橙色：一期独立受控WASM runner；同一部署中的执行隔离，不等于多ASGI worker。
- 绿色：持久事实、当前授权/准入、计算依赖revision与原子结果/outbox。
- 紫色虚线：后续任务业务、蓝图、自研通用流程、多节点与隐私扩展。
- 实线：调用、受权事实读取、计算回执及持久提交；虚线：组件生命周期或后续集成。

目标图中的 RuntimeHost 只展示代表性监督连线。未来 WasmRunnerProvider 应有代次租约与受保护 IPC；无 imports 的 guest 只进行有限纯计算，runner 不持数据库凭据、用户 token 或发布私钥。上述 WASM 组件尚未实现，具体 OS 隔离和实际引擎版本仍需在实现前锁定并验收。

EvaluateQualification在读取事务结束后进行WASM计算，回执不是最终授权。宿主提交前重读当前身份/权限、包状态、行与集合revision，匹配才同事务写结果、命令终态、审计和outbox。计算期间不持业务锁；外部投递在事务外执行，未知结果保留并对账。

现有隔离 IdP 提供密码＋TOTP 真实协议证据，Stage 统一检查策略及认证年龄；原生模块另提供服务端因素绑定与恢复；脚本验证不替代 Maestro 桌面或生产用户恢复流程。具体业务批准拟在 Stage RP WebAuthn 与 IdP step-up 候选中冻结 profile，图不预选凭据方案；开发签名方保留在 runner 之外。普通认证、批准、签名、激活与本次执行许可分别检查。图不表示已接通 Maestro/Virtuoso 企业登录、生产 IAM 或 KMS。

组件内核与未来Workflow Runtime均由Stage自研。长期流程等待保存持久状态，不占用步骤provider租约；每步复用同一授权和命令契约。Graphon只供研究，不在产品运行依赖中。

## 编辑与复现

`stage-architecture.mmd` 是唯一图结构来源。本轮使用已有 Mermaid 11.17.2 与 Chromium 的 Mermaid API 导出 SVG/PNG，不新增项目依赖。结构改变后同时生成各格式并核对文字、箭头与阶段范围。

若另有 Mermaid CLI，可使用等价命令：

```bash
mmdc -i docs/diagrams/stage-architecture.mmd -o docs/diagrams/stage-architecture.svg -b white
mmdc -i docs/diagrams/stage-architecture.mmd -o docs/diagrams/stage-architecture.png -b white -s 2
```

SVG保留可访问标题和描述。文档渲染通过不代表Core、身份、WASM或联调场景已经执行。
