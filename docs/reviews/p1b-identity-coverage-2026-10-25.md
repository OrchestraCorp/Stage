---
title: P1b 身份与授权 I1 场景覆盖矩阵
status: scoped-evidence-review
implementation_status: human-metadata-path-implemented-other-scenarios-explicitly-scoped
review_date: 2026-10-25
policy_effect: none
---

# P1b 身份与授权 I1 场景覆盖矩阵

本文逐项对照[身份规范 I1 清单](../identity-and-authorization.md#92-i1-验收场景)，记录当前代码和已有验证证据的实际边界。它补充[P1b.3 人员操作记录](p1b3-human-work-2026-10-25.md)，不将一个 pytest 函数等同于一个完整 I1 场景，不把内部 fixture、模拟 JWKS 和真实 IdP 验证相加计算“完整 I1 通过数”。矩阵先按源码和已有记录整理，本轮新增测试与实际运行统一记入 [P1b 收尾记录](p1b-closeout-2026-10-25.md)。

一期协作仍为[人员—公司—人员](../people-company-collaboration.md)：业务责任主体是人员；唯一组织根承载授权与审计；Agent 是受监督工具。当前公开写入只支持已有 WorkItem 标题/说明编辑，不能将其验收扩大成任务领取、审批、人员管理、独立 Agent 或全部业务能力已完成。

## 1. 状态与证据口径

| 状态 | 本文含义 |
|---|---|
| `implemented` | 列出的当前路径已有实现和对应测试；只覆盖该行明确限定的部分 |
| `partial` | 场景已有部分实现/证据，但原场景还含未验证或未建设的义务 |
| `unimplemented` | 尚无对应能力实现；拒绝不支持输入不算正向实现 |
| `not-enabled` | 当前装配没有启用该业务入口；内部测试或基础设施能力不能替代上线验证 |
| `deferred` | 用户确认后置的对象/子场景，不计一期缺口，也不计通过 |

来源分为：**真实协议**（本地 Keycloak、真实授权码/PKCE、真实 JWT 和 Stage HTTP）、**应用集成**（真实 IdentityService/loader/kernel/数据库，签名 key 与 JWKS transport 为受控测试数据）、**数据库竞争**（PostgreSQL 两连接与阻塞 PID）、**内部单元/fixture**、**代码审查**。SQLite 结果不证明 PostgreSQL 行锁正确；测试文件存在也不代表所有 opt-in 场景在每次回归中都已运行。

下表用文件代号加精确函数名定位证据。运行环境、测试批次与通过数量沿用[P1b.3 记录](p1b3-human-work-2026-10-25.md)及[首条身份切片记录](p1b-first-slice-2026-10-25.md)，不在本矩阵重算。新增授权结果恢复测试已单独运行于 SQLite：`tests/authz tests/execution` 为 192 passed、6 skipped，其中新增 9 项；6 个跳过是 PostgreSQL 竞争测试，不计通过。后续总回归若纳入这些用例，不能再次累加。

| 代号 | 实际文件与来源 |
|---|---|
| PROTOCOL | [identity_idp_smoke.py](../../scripts/identity_idp_smoke.py)：真实 loopback Keycloak 协议脚本 |
| LIVE | [test_human_work_live.py](../../tests/integration/test_human_work_live.py)：显式 opt-in 的真实 Keycloak → 临时 Stage → 独立 PG 写入链路 |
| AUTH | [test_authentication.py](../../tests/identity/test_authentication.py)：签名 JWT 与受控 JWKS transport |
| IDENTITY | [test_service.py](../../tests/identity/test_service.py)：真实身份服务、数据库与受控 JWKS |
| EVIDENCE | [test_evidence.py](../../tests/identity/test_evidence.py)：服务器签发的内部人员证据及当前性重验 |
| ADMIN | [test_administration.py](../../tests/identity/test_administration.py)：开发身份安装、撤销和恢复 |
| ID-SCHEMA | [test_schema.py](../../tests/identity/test_schema.py)：身份表约束 |
| LOAD | [test_database_loader.py](../../tests/authz/test_database_loader.py)：数据库任职、资源和关系读取 |
| LOAD-GUARD | [test_loader.py](../../tests/authz/test_loader.py)：锁标记、角色摘要及事实版本 |
| ROLE | [test_versioned_decisions.py](../../tests/authz/test_versioned_decisions.py)：固定角色能力判定 |
| AUTHZ-SCHEMA | [test_schema.py](../../tests/authz/test_schema.py)：任职/关系/资源约束 |
| COMMAND | [test_authenticated_commands.py](../../tests/execution/test_authenticated_commands.py)：真实人员证据到事务与结果 |
| RECOVERY | [test_authorization_recovery.py](../../tests/execution/test_authorization_recovery.py)：当前读任职、跨人员结果隔离、到期与冲突边界 |
| HTTP | [test_work.py](../../tests/api/test_work.py)：公开人员操作、错误映射、隐私与 Provider 生命周期 |
| ID-HTTP | [test_http_boundaries.py](../../tests/identity/test_http_boundaries.py)：身份接口边界 |
| ID-PG | [test_concurrency.py](../../tests/identity/test_concurrency.py)：身份读取与撤销的 PG 锁竞争 |
| CMD-PG | [test_human_command_concurrency.py](../../tests/identity/test_human_command_concurrency.py)：已认证人员命令与 profile 撤销的 PG 竞争 |
| AUTHZ-PG | [test_kernel_db_loader.py](../../tests/execution/test_kernel_db_loader.py)：命令与主体、成员、角色版本撤销的 PG 竞争 |
| DELIVERY | [test_worker.py](../../tests/delivery/test_worker.py)：内部受控 Outbox Worker 与模拟接收端 |
| RUNTIME | [test_execution_components.py](../../tests/runtime/test_execution_components.py)：内部命令/投递组件租约与关闭 |

## 2. I1-01～25、35～36 的逐项覆盖

| ID | 当前状态 | 已实现边界、证据与精确用例 | 未覆盖/不应推导的结论 |
|---|---|---|---|
| I1-01 | `partial` | PROTOCOL `authorization_code`、`run` 实际执行授权码＋S256 PKCE、检查固定 redirect/state、验证 ID token nonce，并验证 code 重放、错误/缺失 PKCE 拒绝；LIVE `test_real_idp_human_edit_commit_recovery_and_revocation` 使用该真实流程后调用 Stage。 | Maestro 桌面系统浏览器、回调监听、state/nonce 错配注入、token 安全保管尚待实际客户端联调。脚本的 loopback HTTPX cookie 适配不能作为产品登录实现。 |
| I1-02 | `implemented`，限定已选 JWT profile | AUTH `test_wrong_profile_or_malformed_claims_rejected`、`test_signature_and_algorithm_are_verified`、`test_token_cannot_select_trust_or_other_profile`；IDENTITY `test_self_projection_resolves_the_deployment_organization`；PROTOCOL `run` 与 LIVE 同名主用例提供真实 access token 正向证据。已核验 issuer、audience、用途、client、签名、算法、exp/nbf/iat 和输入格式。 | 仅声明已登记的 Keycloak access-token profile；不代表所有 OIDC 发行方或任意 JWT 都受支持。 |
| I1-03 | `unimplemented` / `not-enabled` | [KeycloakAuthenticator](../../app/identity/authentication.py) 只处理已选 JWT profile，未知 profile/非 JWT 不进入已授权路径；AUTH `test_malformed_json_never_causes_server_error` 等覆盖部分非合法 JWT 拒绝。 | 没有 opaque adapter 或真实 introspection；没有完整 inactive/audience 缺失/远端失败的 introspection 正反向证据。不能把 JWT 解析拒绝记为 opaque 正向通过。 |
| I1-04 | `partial` | PROTOCOL `run` 独立验证客户端 ID token 的签名、issuer/client audience、nonce/sub/用途，再用 access token 调 Stage；AUTH `test_wrong_profile_or_malformed_claims_rejected`；LIVE 主用例确认实际 ID token 不能调用工作项接口。 | 尚无完整真实 refresh token、client assertion、发布签名混入 API 的逐类矩阵；产品客户端 ID token 验证仍待 Maestro 联调。 |
| I1-05 | `partial` | AUTH `test_unknown_kid_refresh_is_bounded_and_rotation_is_supported`、`test_expired_cache_does_not_allow_verification_during_outage`、`test_jwks_failure_is_unavailable`、`test_token_cannot_select_trust_or_other_profile` 验证有界刷新、故障不延长缓存及不跟随 token 指定取钥地址。COMMAND `test_issued_identity_does_not_survive_revocation_before_command_transaction[trust]` 覆盖本地 key revision 变化拒绝旧证据。 | 默认 JWKS 缓存 60 秒；轮换期间仍缓存旧 key 可能继续验证旧签名。尚未完成真实 IdP 新旧 key 重叠/紧急逐 key 撤销全矩阵。本地 revision 变更不能证明即时获知外部 key 撤销。详见第 3 节。 |
| I1-06 | `partial` | AUTH `test_human_evidence_omits_unknown_authentication_strength` 保留缺失 auth_time/acr/amr 为未知，不用 iat 填充；`test_wrong_profile_or_malformed_claims_rejected` 拒绝错误类型；IDENTITY `test_organization_admission_is_not_implied_by_membership` 覆盖来源与 required_acr 条件。 | 当前元数据编辑没有 step-up、新鲜度上限或 MFA 批准动作；不能声明已验证高保障业务批准。 |
| I1-07 | `partial`，可信代理拓扑 `not-enabled` | PROTOCOL `run` 的 `stage_forged_identity_headers_rejected` 验证伪造身份头不替代 Bearer；HTTP `test_proxy_root_path_keeps_identity_and_work_errors_private` 仅证明 root_path 下缓存策略仍生效。 | 尚未部署/验收可信代理链、固定 origin、直连绕过或额外代理跳。root_path 和拒绝伪造头均不证明可信转发拓扑已实现。 |
| I1-08 | `implemented`，限定显式映射；关联流程 `unimplemented` | IDENTITY `test_external_subject_preserves_unicode_and_exact_case`、`test_verified_identity_without_membership_is_denied`；ID-SCHEMA `test_external_identity_exact_subject_cannot_be_linked_to_a_second_principal`；ADMIN `test_existing_principal_is_not_implicitly_linked_and_failed_install_is_atomic`。查询使用可信 provider/namespace/exact subject，不按 email 推导 Principal。 | 没有账户关联、issuer 迁移或已核验同人证明流程。禁止自动合并不等于已经实现受权关联。 |
| I1-09 | `unimplemented` | 固定已登记的 Stage subject namespace；AUTH `test_service_profile_cannot_reuse_human_namespace` 证明人/服务语义不能因相同 sub 混用。 | pairwise broker、多 client/sector 同人映射及批准独立性证明尚未实现；上述类型隔离不是 pairwise 正向证据。 |
| I1-10 | `implemented`，限定单根与授权 scope | ID-SCHEMA `test_deployment_cannot_store_a_second_authorization_root_even_when_the_first_is_disabled`、`test_membership_cannot_retype_its_principal`；AUTHZ-SCHEMA `test_scope_parent_must_exist_in_same_organization_and_not_self`；LOAD `test_scope_and_relation_mismatch_fail_closed`。 | scope 是授权范围，不能据此声明部门/法人组织树或管理 UI 已建设。唯一组织根不产生全员访问权。 |
| I1-11 | `implemented`，限定当前人员命令 | LOAD `test_selected_assignment_is_required_and_assignments_are_not_implicitly_merged`、`test_role_revision_is_pinned_and_revocation_is_immediate`；ROLE `test_empty_pinned_actions_do_not_fall_back_to_a_builtin_role`；RECOVERY `test_current_read_assignment_can_recover_after_write_assignment_revocation_without_merging_grants`；HTTP `test_http_validation_identity_and_cache_boundaries`。 | 当前只支持显式选择任职；没有自动并权或用户自报角色路径，也不承诺完整任职管理 HTTP。 |
| I1-12 | `implemented`，限定直接 ACL；领域关系 `partial` | LOAD `test_database_loader_builds_authorization_from_bound_facts`、`test_scope_and_relation_mismatch_fail_closed`、`test_rejoining_does_not_revive_assignment_from_an_ended_membership`；LOAD-GUARD `test_loader_does_not_turn_an_unrelated_relation_into_authority`。当前只有明确受控的 owner_of/viewer_of。 | assignee/reviewer/eligibility 必须来自未来任务领域事实；没有任意元组赋予这些权限的入口。 |
| I1-13 | `deferred` | [人员协作决策](../people-company-collaboration.md) 后置独立 Agent 与有限委托。 | 旧纯判定 Delegation fixture 不是一期产品能力，不计委托场景通过。 |
| I1-14 | `deferred`，服务认证部分已保留 | IDENTITY `test_workload_needs_an_exact_current_registration`；EVIDENCE `test_service_credential_cannot_issue_human_command_evidence`；HTTP `test_workload_credential_cannot_be_used_as_a_person`。这些证明技术服务身份不冒充人员。 | Agent registration/binding 未实现；既有 workload/service 认证不是独立 Agent 绑定。 |
| I1-15 | `partial` / 业务投递 `not-enabled`；通用 ServiceGrant `deferred` | DELIVERY `test_delivery_requires_current_authorizer`、`test_denied_current_relation_blocks_without_sending`、`test_allowed_decision_must_match_delivery_context`；RUNTIME `test_core_managed_delivery_component_starts_and_persists_receipt`。Worker 构造必须有显式 authorizer、sink 和范围。 | 默认装配未启用真实业务投递 Worker；没有真实 Connector 的服务用途验收。内部受控 fixture 不等于生产服务授权；通用 ServiceGrant 不作一期前置。 |
| I1-16 | `implemented`（人员证据）/ `partial`（技术来源） | EVIDENCE `test_human_evidence_is_frozen_token_free_and_revalidated_without_network`、`test_changed_identity_dependency_rejects_previously_verified_evidence`；COMMAND `test_refreshed_credentials_replay_the_same_command_without_rewriting_original_evidence`；HTTP 输入边界测试拒绝多余 actor/organization/确认字段。`HumanAuthenticationEvidence.audit_snapshot` 显式白名单不含 token/外部 sub。 | frozen 对象不是防恶意 Python 的沙箱；服务目前只有身份读取证据，没有通用服务写命令。 |
| I1-17 | `partial` | [HumanWorkService](../../app/providers/work.py) 每次调用本地构造证据/context/kernel，持有独立 `CallScope` 与 Session；HTTP `test_parallel_people_and_assignments_never_borrow_authority` 以两名真实签名测试人员并行验证任职不可混用与结果隔离；HTTP `test_real_service_timeout_cancels_work_and_releases_runtime_calls`；RUNTIME `test_command_component_uses_core_lease_and_preserves_persistent_idempotency`。 | 已有请求隔离和租约证据，但没有 Maestro 多人员、多任职、故障与恢复组合的完整并发产品验收。 |
| I1-18 | `partial` | HTTP → HumanWorkService → AuthenticatedAuthorizationLoader → CommandKernel 为当前唯一公开业务写路径；HTTP `test_real_identity_provider_http_edit_replay_recovery_and_conflict`；AUTHZ-PG `test_database_loader_without_its_lock_coordinator_rejects_with_no_side_effects`。请求不能提交自行构造的 ExecutionContext。 | SDK/CLI 业务客户端、入队命令未启用；可信 Python 仍能调用内部 handler，受代码评审/装配合同约束，不是语言或数据库沙箱。不能据此声称任意内部代码无法绕过。 |
| I1-19 | `partial` | 当前 WorkItem/operation 返回明确 DTO；HTTP `test_revoked_current_relation_blocks_http_read_replay_and_result_recovery`、`test_safe_failure_mapping_preserves_request_id_and_cache_policy`；RECOVERY `test_another_authenticated_reader_cannot_recover_the_original_callers_operation`、`test_stale_object_version_does_not_disclose_conflict_without_current_action_authority`；ID-HTTP 错误/no-store 测试。 | 搜索、导出、附件、审计浏览、SSE 和真实通知接口尚未启用，不能计入读取投影验收。 |
| I1-20 | `implemented`，限定当前短事务命令 | COMMAND `test_refreshed_credentials_replay_the_same_command_without_rewriting_original_evidence`、`test_authentication_expiry_during_handler_rolls_back_work_result_and_audit`；LOAD-GUARD `test_fact_versions_capture_actual_rows_ancestors_and_set_epoch`；HTTP `test_unknown_commit_returns_original_key_location_and_recovers_without_second_write`。结果、授权证据与审计同事务；没有接受旧 decision 作为凭据的 API。 | 普通可信 handler 不得修改授权依赖；当前最终重验检查身份事实和授权快照时效，不是任意任职/ACL 误写检测器。WASM 输出准入尚未实施。 |
| I1-21 | `implemented`，限定支持的本地撤销路径 | CMD-PG `test_profile_revocation_first_blocks_and_rejects_already_authenticated_command`、`test_command_first_holds_authentication_lock_through_commit_then_revocation_blocks_recovery`；AUTHZ-PG `test_revocation_first_blocks_command_and_denies_after_commit`、`test_command_first_commits_before_waiting_revocation_and_replay_is_denied` 覆盖 principal/membership/role_version。ID-PG 两个方向覆盖身份读取。 | 结论是遵循共同写入协议的本地 PostgreSQL 排序；不覆盖任意外部 SQL、远端 IdP 撤销或未来未接入的 writer。 |
| I1-22 | 人员业务队列 `not-enabled`；独立 Agent 队列 `deferred` | 当前元数据编辑同步执行且不写业务 Outbox；内部 DELIVERY `test_reconciliation_itself_requires_current_authorization`、`test_revocation_after_dispatch_does_not_erase_external_receipt` 区分当前投递与已经提交/发出的副作用。 | 尚无“入队后重新构造人员成员/任职再执行新业务命令”的路径，不能把 Outbox 投递测试当成人员业务队列验收。启用该队列前必须补本行，原 token 不得入队。 |
| I1-23 | `partial` | IDENTITY `test_cached_signature_keys_do_not_bypass_current_revocation`；AUTH `test_expired_cache_does_not_allow_verification_during_outage`；EVIDENCE `test_changed_identity_dependency_rejects_previously_verified_evidence`；COMMAND profile/trust/membership 撤销测试。当前没有 Redis allow-decision 缓存，提交重读 PG。 | 未验收未来政策/授权缓存；远端 IdP 注销、禁用或 JWT 撤销与本地 epoch 无自动同步。证据 TTL 不构成外部撤销上界，见第 3 节。 |
| I1-24 | `partial` | CMD-PG `test_waiting_for_trust_does_not_acquire_principal_or_organization_locks_early`；ADMIN `test_postgresql_revocation_waits_for_shared_identity_reader`；LOAD-GUARD `test_coordinator_marker_cannot_be_reused_after_transaction_end`。当前锁序为 trust → principal → 组织 guard → 业务对象。 | 支持的撤销路径已验；跨 issuer 关联、主体合并、任意多主体批量安全变更没有完整实现/验收，不能宣称所有未来 writer 已被覆盖。 |
| I1-25 | `partial`；账户恢复流程 `unimplemented` | EVIDENCE `test_rejoining_cannot_rebind_old_human_evidence`；LOAD `test_rejoining_does_not_revive_assignment_from_an_ended_membership`；ADMIN `test_historical_bootstrap_digest_replays_both_spellings_without_reopening_identity`；RECOVERY 当前只读任职恢复测试。重新认证不会复活已撤销授权。 | IdP 密码/MFA 恢复、Stage recovery 操作、email 转移治理和恢复后高保障重认证没有真实流程验收。字段 recovery_epoch 不等于恢复产品已建设。 |
| I1-35 | `partial` | 默认 [ApplicationRuntime](../../app/bootstrap/application.py) 装配真实 storage/identity/human-work，不启动 fixture 或自动 bootstrap；ADMIN `test_bootstrap_persists_only_explicit_identity_facts_and_replays_without_grants`、`test_cli_refuses_production_without_leaking_connection_secret`。公开 Work 路径使用真实证据与数据库 loader；LIVE 与模拟测试分开记录。 | 开发 CLI 拒绝 prod 不等于 RuntimeHost 无法加载任何可信测试 PluginSpec。通用内部 `command_plugin` 仍接受显式可信 loader；没有覆盖所有插件的 prod fixture 识别/封锁机制。fixture 仅内部测试/显式 dev/test 安装，不能声称完整 I1-35 已通过。 |
| I1-36 | `partial` | EVIDENCE 冻结/无 token 测试；COMMAND 原审计保留测试；当前查询只装载目标主体、该成员经历/所选任职、角色版本、资源 scope 链、必要 ACL 和当前 WorkItem；公开 DTO 不输出内部证据或私人会话。 | 一期尚无资格证明存储/最小 WASM 投影、细粒度审计读取接口及完整数据最小化验收；不能将元数据读取覆盖扩大到全部业务资料。 |

## 3. 撤销和缓存的精确边界

**本地 PostgreSQL 撤销已经有排序证据。** 对已支持的 principal、membership、profile、role_version 等写方，命令与撤销按共同锁序串行：撤销先完成，后续旧证据/旧授权拒绝；命令已持锁，则撤销等待该事务结束。随后读取、幂等重放和结果恢复仍重新核验当前权限。Redis 失效消息没有参与这个正确性前提。

**远端 IdP 会话状态不是本地撤销事实。** 当前 Stage 离线验证 JWT，没有 introspection、back-channel logout 或账户禁用事件同步。IdP 注销、禁用账号、撤销 refresh token 后，已经签发且尚未到期的 access JWT 可能继续通过 Stage 验证。刷新认证产生新的短期 evidence，也不会自动询问远端账户状态。默认 evidence TTL 60 秒只约束该份内部证据的使用，不能写成“远端撤销最多 60 秒生效”。本轮 LIVE 已实测：实际注销后 refresh token 交换返回 400，未到期 access token 读取工作项仍为 200，随后本地任职撤销使读取返回 403；包含该增量的 PG 定向组合 56 项通过，最终完整回归另见收尾记录。

**JWKS key 缓存与账户会话撤销也是两件事。** 默认 JWKS 缓存 60 秒、未知 kid 刷新节流 5 秒；缓存未过期时合法缓存 key 可以继续用于签名验证。JWKS 移除旧 key 的观察时间受缓存/刷新影响，并且已经产生的内部 evidence 仍有自己的有效截止；不能把单个 JWKS TTL 当成所有业务提交的统一外部撤销上界。缓存过期而取钥失败时拒绝建立新的认证证据，不无限延长旧缓存。现有本地 profile 撤销可阻断调用；trust/key revision 的可信本地变更使旧 evidence 不匹配，但不代表远端 key 变化会自动写入该 revision。

生产接入时应选定并验收 access-token 最大寿命、IdP 注销/禁用语义、key 轮换重叠期与紧急阻断流程；如需要更快观察外部状态，再评审 introspection 或受信事件同步。以上均不能由当前测试 JWT 或数据库锁自动推导。

## 4. 后续验收入口

- Maestro 先完成真实桌面授权码＋PKCE、回调、state/nonce 错配、token 保管及刷新后的相同幂等键联调；当前脚本不替代桌面客户端。
- 新增 issuer、opaque、pairwise broker 或可信代理拓扑时，先扩展该 profile 的完整正反向证据；当前 `unimplemented/not-enabled/partial` 状态不得直接改为“通过”。
- 启用人员业务队列或真实 Connector/Worker 时补 I1-15/18/19/22 的对应入口与恢复场景，不通过复用已有 Outbox 测试跳过当前人员重验。
- I1-26～34 属后续 Stage RP/批准、签名准入与 WASM/资格评估联调：本轮均未验收，不能用普通登录、元数据编辑或现有组件测试代替。独立 Agent/Delegation 继续后置。
- 受控 handler 仍属于可信代码边界：禁止修改任职/ACL/角色版本等授权依赖，专项身份映射误变更回滚测试不构成通用安全事实写入沙箱。

本矩阵没有“36/36 通过”或“完整 IAM 已完成”的结论；当前可交付范围是选定 JWT profile 下，人员对既有工作项的读取、受控元数据写入、当前授权与原结果恢复。
