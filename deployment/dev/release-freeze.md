# 冻结与重现已验收版本

开发设备的 systemd 服务可以继续挂载工作区，便于 Stage 研发；工作区服务不能自动代表某个历史 Git HEAD。交付时用下面的独立步骤将源码、依赖锁、迁移和制品绑定到同一个提交。工具不会提交代码、修改数据库或切换服务。

1. 完成 PostgreSQL、Chromium、Runtime 与静态/合同检查，审查变更并提交。`git status --short` 必须为空。
2. 执行 `uv run python scripts/freeze_release.py --output /opt/stage/releases/<本次提交>`；输出目录必须尚不存在，且在工作区之外。
3. 留存 `stage-source.tar.gz`、`dist/` 和 `manifest.json`。manifest 包含精确 Git revision、Python/uv 版本、`uv.lock` 与浏览器锁摘要、迁移 head、各制品 SHA-256。发布前按 manifest 校验文件。
4. 将源码归档解压到新目录，执行 `uv sync --locked --dev`，再运行合同、静态检查和选定验收；不得借用原工作区的导入路径。生产安装可以只用 `uv sync --locked --no-dev`，SQLite/Playwright 属于开发依赖。
5. 切换运行版本应显式使用该只读目录、数据库外配置/密钥和明确的迁移计划。开发 service 原来直接挂载工作区，不会被冻结工具偷偷改成发布服务。

源码归档直接来自 `git archive <revision>`，不包含忽略的 `.env`、私有密钥、运行数据库或工作区临时改动。gzip 时间固定，构建使用提交时间作为 `SOURCE_DATE_EPOCH`，构建器由 pyproject 固定。重复构建应比较 manifest 中的制品摘要；跨平台的 wheel/依赖解析仍需在目标 Python/操作系统再次验证。wheel 包含应用和启动检查需要的 migrations；运行手册、Alembic CLI 配置和锁文件以源码归档为准。CI 构建 wheel 后执行 `scripts/check_distribution.py`，在独立目录验证包导入、路由和迁移 head，避免源码目录掩盖缺失的打包资源。

CI 中的 SQLite 快测、PostgreSQL 真库和 Chromium 门禁分开运行。GitHub/GitLab 浏览器作业使用专属一次性 PostgreSQL，并执行 `scripts/check_browser_acceptance.py`：两个真实浏览器场景必须执行，缺少依赖、跳过或失败均不能通过。仅上传经过裁剪的 JUnit 名称、时间与状态；不上传凭据 fixture、浏览器存储、页面或 trace。

本地运行与 CI 相同的浏览器门禁：

```bash
npm ci --prefix scripts/browser --ignore-scripts
npm exec --prefix scripts/browser -- playwright install --with-deps chromium
# TEST_DATABASE_URL 必须指向获准重置的一次性 PostgreSQL；不要指向开发业务库。
STAGE_ALLOW_TEST_DATABASE_RESET=1 uv run python scripts/check_browser_acceptance.py
```

默认选择仓库锁定的 Playwright 模块；`STAGE_PLAYWRIGHT_MODULE` 只用于明确的本地安装覆盖。`TEST_DATABASE_URL` 未提供时不会回退到 SQLite。共享 PostgreSQL fixture 禁止多 worker 并行重置；将来需要并行时，先设计每个 worker 的独立数据库。
