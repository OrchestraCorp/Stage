# 当前云开发设备

本设备的 P1a 服务使用单 ASGI worker，源码路径 `/root/OrchestraCorp/Stage`。三个 systemd 开发服务均以 `orchstage` 非登录系统用户运行，已启用开机启动和失败重启：

| 服务 | 监听 | 用途 |
|---|---|---|
| `orchestra-stage-dev` | `127.0.0.1:8080` | Stage `/health`、`/ready`、开发 OpenAPI |
| `orchestra-stage-postgres-dev` | `127.0.0.1:15432` | PostgreSQL 17.11，开发库 `stage` |
| `orchestra-stage-redis-dev` | `127.0.0.1:16379` | Redis 8.2.3，缓存/辅助基础设施 |

```bash
systemctl status orchestra-stage-dev orchestra-stage-postgres-dev orchestra-stage-redis-dev
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/ready
systemctl restart orchestra-stage-dev
journalctl -u orchestra-stage-dev -n 80 --no-pager
```

修改代码后重启 Stage。当前 systemd 单元通过只读 bind mount 将工作区映射到 `/srv/orchestra-stage`，运行用户不获得 `/root` 全目录访问。单元模板见 [orchestra-stage-dev.service](orchestra-stage-dev.service)。PostgreSQL/Redis 二进制位于 `/opt/orchestracorp/stage-dev`，数据目录为 `/var/lib/orchestracorp-stage-dev`；来源沿用存储验证时核验的官方 PGDG 包与 Redis 源码，没有安装 Docker daemon。

连接配置位于设备专用的 `/etc/orchestra-stage-dev.env`（root 0600），使用本地生成密码，不提交仓库。只在受信任终端加载它：

```bash
set -a
. /etc/orchestra-stage-dev.env
set +a
cd /root/OrchestraCorp/Stage
.venv/bin/python -m alembic upgrade head
```

迁移是独立部署操作，Core 启动仅核验 schema，不自动迁移或创建业务表。测试使用另一个 `stage_test_core` 库及显式重置授权，不能把 `stage` 开发库传给重置测试。Docker 可用的其他设备继续使用仓库 `compose.yaml`，不需复制此机数据或凭据。

`/health` 只报告进程存活。必需 PG 或 schema 不可用使 `/ready` 返回 503，连接恢复后探测自动恢复就绪；默认 Redis 可选，故障只标记 Redis unavailable。需要 Redis 的应用组合须明确设置 required 或声明相应组件依赖。尚无真实 IAM，因此服务只开放系统探针和开发契约；命令/投递组件仅接受由可信组合提供的 loader、authorizer、sink 和租户清单，验收 fixture 不作为生产适配器。

本地访问可使用现有 SSH 通道转发 8080；不公开数据库或 Redis 到公网。该设备配置是开发运行环境，P1b 身份、P1c WASM/签名以及生产备份恢复仍按各自验收推进。
