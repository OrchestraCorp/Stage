# Stage Contracts

`openapi.yaml` 是当前 HTTP 契约入口。新增或修改 API 时，同步更新实现、契约、稳定 operationId 和测试。

业务接口使用 URL 大版本 `/api/v1`；系统探针 `/health`、`/ready` 保留根路径。当前包括身份读取、本人任职发现，以及人员工作项读取、元数据编辑与操作结果查询。同一大版本保持兼容，破坏性字段或行为变更进入新大版本；未实现的 `/api/v2` 返回 404。

Stage 是企业私有化部署。每个实例由服务端唯一组织授权根决定；该根是信任和治理边界，不等同于一个法人公司，也不提供 SaaS 租户选择。`/identity/me` query 仅接受 `profile_ref/profile_version`，响应使用 `organization_ref`；额外 query（含 `tenant_ref/organization_ref`）返回 422。尚无正式客户端的首版冻结同步删除旧 `/api/identity/me`，不提供旧路由或重定向。组织根、组织单元与 scope 的边界见[组织授权根语义](../docs/organization-root-semantics.md)。

`GET /api/v1/identity/assignments`（operationId `identity_list_self_assignments`）仅供当前 human，query 为 `profile_ref/profile_version/limit/after`；limit 默认 20、最大 50。返回 `{items, next_after}`，仅含本人固定成员经历下的有效任职、角色和 scope 选择。采用有界候选分页，过滤失效角色/scope 后可出现 `items=[]` 且 `next_after` 非空；继续到 cursor 为 null，不保证跨页快照，也不返回合并权限。

组织 MFA 策略启用后，所有 human 入口按精确 profile/version、ACR、AMR 和 auth_time 年龄校验。强度不足或需重新登录时返回 401 / `MFA_REQUIRED`，`WWW-Authenticate: Bearer error="insufficient_user_authentication"`；客户端根据可信部署配置发起强认证，不通过选择旧 profile 或只刷新 token 绕过。服务自身读取仍按独立技术准入。详见[登录 MFA](../docs/login-mfa-development.md)。

人员工作项路由为 `GET /api/v1/work-items/{work_item_id}`、`PUT /api/v1/work-items/{work_item_id}/details` 和 `GET /api/v1/work-items/{work_item_id}/operations/{idempotency_key}`。query 只接受 `profile_ref/profile_version/assignment_ref`；服务端验证人员身份与明确单任职，service token 不能使用这些业务接口。PUT 另要求 `Idempotency-Key` 和严格 body `title/description/expected_version`；成功与错误均 `no-store`。提交未知时使用原 key 查询 Location，恢复仍检查当前权限。

HTTP URL 大版本、OpenAPI `info.version: 1.0.0` 合同发行号、OpenAPI 规范版本、认证 `profile_version` 和数据行 `version` 含义分别定义，不能相互替代；项目包版本也独立管理。当前没有个人资料 CRUD、人员列表、角色管理、工作项创建或完整任务协作接口。详细输入、错误与范围见[私有化部署与 API v1](../docs/private-deployment-api-v1.md)和[P1b.3 记录](../docs/reviews/p1b3-human-work-2026-10-25.md)。

修改合同后运行 `uv run python scripts/export_contract.py --write` 导出，再执行 `uv run pytest -q tests/contract` 核对实际 FastAPI/OpenAPI 与稳定操作/错误边界。`export_contract.py` 不带 `--write` 只打印合同，不作为验收检查。
