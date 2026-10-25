# Stage Runtime Core

Core 只使用 Python 标准库，运行于单事件循环。它管理后端组件、依赖、运行资源和调用，不解释 Agent 会话、当前用户、组织身份、业务命令或数据库事务。应用适配位于 `app/providers` 和 `app/bootstrap`。

`ComponentContext` 是组件运行上下文：它表示某个组件激活代次可以解析哪些服务、登记哪些资源、启动哪些进程内任务，以及如何停止和收尾。它不是 HTTP request context、`ExecutionContext`、`AuthorizationContext`、数据库事务或 Workflow context；其中不得保存当前用户、access token、当前组织、授权结果或业务状态。`owner_id` 表示运行资源归属，`generation` 表示组件激活代次，二者都不是业务主体或业务对象版本。

```python
from app.runtime import ComponentContext, PluginSpec, RuntimeHost, ServiceKey

VALUE: ServiceKey[dict[str, int]] = ServiceKey("example", "value", "1")


def provide(ctx: ComponentContext) -> None:
    ctx.provide(VALUE, {"value": 1})
    ctx.effect(lambda: lambda: None, label="example cleanup")


async def example() -> None:
    host = RuntimeHost()
    host.load(PluginSpec("value", provide, provides=(VALUE,)))
    try:
        report = await host.await_ready([VALUE], timeout=2)
        if not report.ready:
            raise RuntimeError("required service unavailable")
        async with host.call(VALUE) as call:
            assert call.require(VALUE)["value"] == 1
    finally:
        closed = await host.close(timeout=2)
        if not closed.successful:
            raise RuntimeError("resources have not all closed")
```

组件通过 `PluginSpec.requires/provides` 声明服务；`inject` 为已声明依赖提供最近层配置。`ctx.require()` 使用当前 committed binding，`ctx.get()` 只是实时观察。调用应使用 `host.call()` 或 `ctx.call()` 固定代次，事务/session 结束后再退出调用作用域。

`ctx.effect()` 在 setup 之前登记 owner，支持 disposer、同步/异步生成器和 context manager。`ctx.spawn()` 管理后台任务，`ctx.on_stop()` 先通知停止生产；资源 disposer 在旧调用和依赖者收敛后执行。存在释放依赖时用 EffectScope 的 `depends_on` 表达，失败不得继续释放仍被使用的资源。

`ctx.isolate()` / `intercept()` 创建同 owner 的视图，`ctx.plugin()` 才建立 child。provider 故障或可用性通知触发受影响依赖重组；同实例 restart/默认 replace 先清理旧代次。handle、登记 ID 和代次分别验证，旧 context 不能向新代登记资源。

`ctx.spawn()` 只监督进程内、可在重启后重建的任务。需要重启恢复的工作必须写入 PostgreSQL 的 outbox/attempt，并由 Durable Worker 使用持久 lease、fencing、幂等键、回执和 `unknown` 对账；不能用 Task、EventBus 或 Redis 队列把临时任务伪装成持久队列。连接器的外部调用应有 deadline、取消策略、稳定幂等身份和结果查询，超时只能记录未知，重启后先查询再决定是否重试。

Runtime Core、Execution Kernel 和未来 Workflow Runtime 分层：Core 管组件和连接器生命周期，Execution Kernel 管一次执行的认证、授权、短事务、幂等、审计和 outbox，Workflow Runtime（P3）持久化 run/step/attempt/wait/recovery。工作流状态不能放进 ComponentContext、Effect 或内存 Fiber；Core 的资源关闭也不会回滚已提交业务事实。

provider 健康探测变化可以触发受影响组件重协调，使不可用 provider 不再发放新调用。当前健康抖动、readiness 变化和真正实现换代仍共享部分路径；后续应分别验证退避、拒绝新调用、排空和 `replace`，不能把一次健康恢复解释为配置已完成换代。

事件使用 `app.runtime.events.EventKey` 和显式 Continue/Stop。snapshot 持有全部 listener 的调用 pin，退出时等待在途回调。回调若需关闭自身、依赖或同 dispatch 中的 owner，使用 `request_close()` 后返回；不能等待自己的关闭链。

关闭超时返回 pending/blocked 报告，host 保留任务所有权；并发等待者共享清理结果。一个等待者取消不会取消共享关闭。调用者应检查报告，不能在超时后自行强关资源并报告完成。运行期失败通过 `snapshot()`、readiness 和 observations 可见；诊断含内部组件信息，不作为未认证 HTTP 响应。

行为规格见 [Core 一致性规格](../../docs/runtime-conformance.md)、[框架实施规格](../../docs/framework-foundation.md)，运行时演进边界见[运行时演进与能力组合决策](../../docs/runtime-evolution-and-composition.md)，场景映射位于 `tests/runtime_core/scenarios.json`。映射通过必须引用真实 pytest JUnit 结果；固定 Cordis 参考与 Stage 特有规则分别留证，不宣称官方跨语言认证。
