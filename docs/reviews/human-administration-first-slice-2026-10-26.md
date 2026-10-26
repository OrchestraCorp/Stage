---
title: 首个人员管理切片实现与架构复查
status: implemented-retested-and-development-service-verified
date: 2026-10-26
policy_effect: evidence-for-bounded-management-slice
---

# 首个人员管理切片实现与架构复查

## 1. 交付边界

本轮按[冻结合同](../human-administration-first-slice.md)补齐首个正式人员管理员入口：本机提名已有人员、本人近期 MFA 接受、当前人员目录、清单内普通任职授予／撤销、本管理域审计与原人原键恢复。Maestro 管理界面、员工激活／离职、多管理员与管理权转交仍未交付。

相较起点 `5df49e15f7c9c55b9aebc90cfdb0612a0fc38715`，新增一个默认管理 provider、9个HTTP操作和一张 `management_authority` 表；合计34个HTTP操作、38张应用表，迁移head `a7d4e2c9f501`。原25个HTTP操作及既有OpenAPI schema语义逐项对比一致。独立wheel可以导入，包含新迁移。

工作区代码与可重现发布冻结分别记录；本轮不将未提交工作区冒充已有发布提交。

## 2. 实现分工

| 文件 | 责任 |
|---|---|
| [api/administration.py](../../app/api/administration.py) | 9个端点、显式profile／任职选择、请求边界、错误／恢复地址与no-store |
| [providers/administration.py](../../app/providers/administration.py) | 真实认证、依赖调用租约、请求总预算与领域服务适配 |
| [administration/service.py](../../app/administration/service.py) | 首管接受、人员／任职投影、有限授撤、安全写事务和提交前重验 |
| [authz/management.py](../../app/authz/management.py) | 管理能力与授予上限判定、角色摘要、当前来源绑定和近期MFA |
| [administration/journal.py](../../app/administration/journal.py) | human actor的原子审计、单一管理幂等命名空间与原键结果 |
| [administration/bootstrap.py](../../app/administration/bootstrap.py) | 本机显式首管提名、pending版本替换及接受后永久关闭初始化 |
| [models/administration.py](../../app/models/administration.py) | 提名／接受、精确身份绑定和固定可授清单的持久事实 |
| [初始化工具](../../scripts/administration_bootstrap.py) | 明确部署、私有计划文件、OS操作责任与未知结果恢复 |

`app.control` 的环境核验与结果查询参数可接受已经验证的部署环境字符串，原有开发计划的dev/test限制不变；新提名工具可以处理已provisioned的生产基础，不创建生产身份或绕过旧工具限制。

## 3. 安全与工程复查

- **统一身份。** 没有新增管理员密码库或登录协议。管理者仍是当前human／成员，并通过原生或明确准入的精确来源认证；管理任职发现不使用手填引用。
- **使用权与授予权分开。** 管理角色持固定四项管理action；可授清单固定普通role revision、内容摘要、精确scope与期限。工作项loader拒绝管理role的独立policy release，管理接口拒绝普通业务任职。
- **初始化不产生隐式超级用户。** 迁移只建空表。本机提名创建suspended任职，必须本人接受；accepted_at保留后，停用／撤销不会重新打开初始化。尚未接受时重提名撤销旧候选任职并留痕。
- **当前权限与时间。** 管理读写都要求组织策略required、密码＋TOTP及最近300秒认证；锁等待和刷新不会延长认证时间。所有请求在锁后与返回／提交前核验身份、来源、管理任职和MFA，原生会话撤销可阻断旧token。
- **事务和并发。** 复用命令键、Trust／Principal保护、组织S/X及managed UoW。目标Principal保护在组织锁前取得；管理安全写使用X，读取使用S。无外部调用或人员等待占用事务。
- **幂等与未知结果。** 所有管理动作共用原人员的管理命令键命名空间，动作／目标／输入／所选管理任职进入摘要。COMMIT开始后的错误或取消返回unknown；原键查询等待在途提交并重新验证当前管理权限。
- **审计隔离。** 人员journal使用human actor和脱敏认证快照，OS journal保留bootstrap_operator；管理审计只开放本切片人类操作，不开放任意认证事件或业务日志。
- **边界拒绝。** 请求体有大小上限；重复JSON/query/认证头、额外组织／actor字段、布尔版本、无时区日期、非法Unicode等均拒绝。错误和未知路径也禁止缓存。

复查修复两处问题：一是普通业务角色可用性过滤会隐藏管理界面的失效角色任职，现保留事实投影并保持授权独立；二是引用字段补显式UTF-8校验，非法代理字符在HTTP输入层拒绝，不落到数据库编码异常。还统一新管理版本计数的int32上限，耗尽时不做部分更新。

## 4. 验证记录

最终源码摘要、依赖版本和验证记录见[机器证据](human-administration-validation-2026-10-26.json)。

| 验证 | 结果 |
|---|---|
| PostgreSQL全量 | **1701通过，10项按环境跳过**；[JUnit摘要](human-administration-postgresql-2026-10-26.xml) |
| SQLite快测全量 | **1600通过，103项按环境跳过**；[JUnit摘要](human-administration-sqlite-2026-10-26.xml)；不是运行库兼容承诺 |
| 新增管理范围 | PostgreSQL全量中新增154项，含首管、领域、HTTP、原生认证与8项真实PG竞争；不将跨数据库重复验证累加为独立功能数量 |
| Chromium | **2场景通过、0跳过**：因素恢复及SSO／同意／form_post／确认退出；[摘要](human-administration-chromium-2026-10-26.xml) |
| Runtime | 全量JUnit中的46／46场景通过 |
| 迁移 | PostgreSQL空库upgrade/check/downgrade/upgrade/check全部通过；SQLite兼容迁移和非空管理事实拒绝降级分别测试 |
| 静态与合同 | Ruff通过；284文件格式通过；Mypy280源文件通过；OpenAPI检查与原有path/schema不变性对比通过 |
| 交付包 | wheel可独立导入，migration head为a7d4e2c9f501 |
| 开发服务 | 私有备份后迁移并重启；health/ready均200；管理入口无凭据401、未知路径404且no-store；5项在线探针与9项原生认证冒烟通过 |

实际开发库management_authority仍为空，迁移未自动提名或授予任何管理员。完整人员管理正向链在隔离真实PostgreSQL与ASGI/原生密码＋TOTP／PKCE路径验证，包括真实COMMIT后丢失响应及会话撤销后的旧token拒绝。浏览器场景和原生HTTP管理场景分别留证，不将前者写成已存在Maestro管理页面。

## 5. 保留的明确限制

1. 首版一名组织级首管；管理域与使用域不合并，但尚无部门人员归属模型、多管理员或管理权转交。
2. 目录展示已有人员标识和状态；姓名、员工编号、人事部门目录及完整个人资料尚未实现。
3. 普通任职自然到期后需要显式撤旧再授新；本轮不增加隐式续期。失效角色／scope的清理需要受控修复，不自动扩大授予范围。
4. 首切片HTTP不能修改管理任职或自身权限，因此保护该路径的最后管理入口；不宣称已经覆盖OS操作、凭据丢失、未来停用／离职等全部末位管理员和应急恢复场景。
5. 首管提名工具要求已有已配置人员与认证基础；生产安装器、密钥／备份恢复、员工激活、组合离职和Maestro实际交互继续按后续切片验收。

新增复杂度集中在具体管理规则与产品接口，没有新增常驻服务、存储系统、运行时层级、通用策略语言或工作流平台。Core保持组件生命周期职责，安全事实变更沿用现有协调边界；本轮不是完整P1b退出或生产放行。
