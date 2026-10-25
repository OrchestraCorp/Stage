# 架构图

[当前代码架构与 Cordis 对照](../current-code-architecture.md) · [当前代码 Mermaid 源](stage-current-code.mmd)：按实际实现区分默认服务和显式装配的内部执行组件；身份入口为 `/api/v1/identity/me`，组织授权根由服务端固定，部门/项目使用 scope。

[主架构 PNG](stage-architecture.png) · [SVG 矢量版](stage-architecture.svg) · [Mermaid 可编辑源](stage-architecture.mmd)

图对应本轮[基座架构](../architecture.md)、[一期方案](../phase-1.md)、[核心对象](../core-object-contracts.md)及[长期路线](../roadmap.md)。SVG/PNG 是一期完整目标图；当前代码另用上方 `stage-current-code.mmd` 与文档内联图描述，P1b 首条认证/成员读取的实现与验证见[实施记录](../reviews/p1b-first-slice-2026-10-25.md)。WASM 与签名准入继续后续阶段，不能把目标图作为已部署证明。

## 图例与执行边界

- 蓝色：一期可信Stage宿主，包括自研Core、身份、准入、输入准备、命令与事务提交。
- 橙色：一期独立受控WASM runner；同一部署中的执行隔离，不等于多ASGI worker。
- 绿色：持久事实、当前授权/准入、计算依赖revision与原子结果/outbox。
- 紫色虚线：后续任务业务、蓝图、自研通用流程、多节点与隐私扩展。
- 实线：调用、受权事实读取、计算回执及持久提交；虚线：组件生命周期或后续集成。

RuntimeHost只展示代表性监督连线，实际管理全部可信组件。WasmRunnerProvider有代次租约与受保护IPC；无imports的guest只进行有限纯计算。runner不持数据库凭据、用户token或发布私钥。具体OS隔离和实际引擎版本仍需在实现前锁定并验收。

EvaluateQualification在读取事务结束后进行WASM计算，回执不是最终授权。宿主提交前重读当前身份/权限、包状态、行与集合revision，匹配才同事务写结果、命令终态、审计和outbox。计算期间不持业务锁；外部投递在事务外执行，未知结果保留并对账。

IdP测试环境与WebAuthn RP提供真实协议验证所需证据，开发签名方保留在runner之外。普通认证、批准、签名、激活与本次执行许可分别检查。图不表示已接通Maestro/Virtuoso企业登录、生产IAM或KMS。

组件内核与未来Workflow Runtime均由Stage自研。长期流程等待保存持久状态，不占用步骤provider租约；每步复用同一授权和命令契约。Graphon只供研究，不在产品运行依赖中。

## 编辑与复现

`stage-architecture.mmd` 是唯一图结构来源。本轮使用已有 Mermaid 11.17.2 与 Chromium 的 Mermaid API 导出 SVG/PNG，不新增项目依赖。结构改变后同时生成各格式并核对文字、箭头与阶段范围。

若另有 Mermaid CLI，可使用等价命令：

```bash
mmdc -i docs/diagrams/stage-architecture.mmd -o docs/diagrams/stage-architecture.svg -b white
mmdc -i docs/diagrams/stage-architecture.mmd -o docs/diagrams/stage-architecture.png -b white -s 2
```

SVG保留可访问标题和描述。文档渲染通过不代表Core、身份、WASM或联调场景已经执行。
