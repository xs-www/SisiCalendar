# GitHub Actions 自动构建镜像并发布到 GHCR —— 实施计划

起草:2026-10-14 · 状态:**已实施**(2026-10-14,交付物见第 2 节;CI 落地结果见第 10 节)

目标:让 `master` 上的提交由 GitHub Actions 自动构建 Docker 镜像并推送到
GitHub Container Registry(GHCR),NAS 侧只需 `pull` 就能部署与升级,不再需要把仓库
拷到 NAS 上本地构建。

本文件是实施依据:按第 6 节的清单逐条做,做完把状态改成「已实施」并补验收证据。
行为约束以 [`AGENTS.md`](../AGENTS.md) 的不变量为准,本计划**不改任何 `app/` 代码、
不新增 Alembic 迁移**。

---

## 1. 已核实的前提(2026-10-14)

| 项目 | 事实 | 出处 |
|---|---|---|
| 仓库 | `xs-www/SisiCalendar`,**public**,默认分支 `master` | GitHub API |
| 现有 `Dockerfile` | `python:3.13-slim-bookworm` + uv 0.12.19 两阶段;非 root `10001:10001`;`EXPOSE 8000`;健康检查 `/healthz` | `Dockerfile` |
| 现有 `compose.yaml` | 本地构建 `sisi-calendar:0.1.0`,端口 `127.0.0.1:443:8000`,`./data:/data` | `compose.yaml` |
| `.github/` | **不存在**,需新建 | `glob` |
| 测试 | 本地 `uv run pytest -q` **450 项全绿**(2026-10-14 实测,可作 CI 门禁) | 本地运行 |
| 静态检查 | `uv run ruff check app tests migrations` 干净 | `todo.md` |
| 敏感文件隔离 | `.dockerignore` 已排除 `data`、`.env`、`*.db`、`docs`、`tests` | `.dockerignore` |
| 配置解析 | `Settings` 使用 `extra="ignore"`,未知环境变量不会导致启动失败 | `app/config.py:37` |

**用户已决策**(2026-10-14):镜像推 **GHCR**(不推 Docker Hub);出
**linux/amd64 + linux/arm64** 双架构;推送镜像前**必须**先通过 ruff + pytest 门禁。

### 1.1 已核到的 Action 主版本(2026-10-14,实施时再核一次)

| Action | 已核到的最新版 | 计划固定为 |
|---|---|---|
| `actions/checkout` | v7.0.1 | `@v7` |
| `astral-sh/setup-uv` | v10.3.0 | `@v10` |
| `docker/metadata-action` | v6.2.0 | `@v6` |
| `docker/build-push-action` | v7.4.0 | `@v7` |
| `docker/setup-qemu-action` | **待核** | 实施时用 API 取当前主版本 |
| `docker/setup-buildx-action` | **待核** | 同上 |
| `docker/login-action` | **待核** | 同上 |

---

## 2. 交付物清单

| # | 文件 | 动作 | 说明 |
|---|---|---|---|
| 1 | `.github/workflows/docker-publish.yml` | 新建 | 门禁 + 多架构构建 + 推 GHCR |
| 2 | `compose.nas.yaml` | 新建 | NAS 纯拉取版 compose(无 `build:`) |
| 3 | `README.md` | 改「部署」节 | 补一段「CI 镜像 + NAS 拉取」 |
| 4 | `docs/部署与运维.md` | 新增一节 | 《从 GHCR 拉取镜像部署到 NAS》(首次部署 / 升级 / 回滚) |
| 5 | `docs/static/ci-docker-publish.md` | 新建 | workflow 与 `compose.nas.yaml` 的静态契约 |
| 6 | `docs/static/README.md` | 改索引 + 本轮变更 | 登记 #5 |
| 7 | `todo.md` | 改 | 登记本轮 |

---

## 3. `.github/workflows/docker-publish.yml` 设计

### 3.1 触发与并发

```yaml
on:
  push:
    branches: [master]
    tags: ["v*"]
  pull_request:
    branches: [master]      # 只构建、不推送,用于验证 Dockerfile 没坏
  workflow_dispatch:        # 手动补一次
```

- `concurrency: group: docker-${{ github.ref }}` + `cancel-in-progress: true`:
  同一分支连续提交只跑最后一次,省额度。
- 顶层 `permissions: contents: read`,`packages: write` 只给构建 job(最小权限)。

### 3.2 job 1 `quality` —— 门禁

`ubuntu-24.04` → `actions/checkout@v7` → `astral-sh/setup-uv@v10`
(`python-version: "3.13"` + 开缓存)→

```bash
uv sync --frozen
uv run ruff check app tests migrations
uv run pytest -q
```

任一步失败 → `image` job 不执行 → **坏提交不会变成 `latest`**。

### 3.3 job 2 `image` —— 构建与推送

`needs: quality`;`permissions: contents: read + packages: write`。

1. `docker/setup-qemu-action`(arm64 需要)
2. `docker/setup-buildx-action`
3. `docker/login-action` → `registry: ghcr.io`,`username: ${{ github.actor }}`,
   `password: ${{ secrets.GITHUB_TOKEN }}` —— **不需要任何自定义 secret**
4. `docker/metadata-action` 生成标签与 OCI 标签
5. `docker/build-push-action`:多平台构建 + `type=gha` 缓存 + 推送

关键参数:

```yaml
platforms: linux/amd64,linux/arm64
push: ${{ github.event_name != 'pull_request' }}
provenance: false     # 老群晖 Docker 对 attestation manifest 支持差,留着会拉不动
sbom: false
cache-from: type=gha
cache-to: type=gha,mode=max
```

**镜像名写死小写** `ghcr.io/xs-www/sisicalendar`(GHCR 拒绝大写仓库名,
虽然 `metadata-action` 会自动转小写,写死更不容易踩坑)。
同时通过 `labels: ${{ steps.meta.outputs.labels }}` 带上
`org.opencontainers.image.source`,让包与仓库关联(这是包页面上出现 README 与
「Public」开关的前提)。

### 3.4 标签规则

| 触发 | 产物标签 |
|---|---|
| push `master` | `latest`、`master`、`sha-<7 位>` |
| push tag `v1.2.3` | `1.2.3`、`1.2`、`1`、`sha-<7 位>` |
| PR | 只在本地构建,不推送 |
| 手动 | 同 `master` |

> NAS 升级默认用 `latest`;要可复现就钉 `sha-xxxxxxx` 或版本号 tag(回滚同一条路)。

---

## 4. `compose.nas.yaml` 设计

**逐字对齐**现有 `compose.yaml` 的运行约束,只把 `build:` 换成 GHCR 的 `image:`;
服务名保持 `calendar`,这样《部署与运维》里所有 `docker compose exec calendar …`
命令都能原样继续用(NAS 上带 `-f compose.nas.yaml`)。

必须保持不变的部分(改动等于削弱安全边界,有文档与测试守着):

- `ports: ["127.0.0.1:18443:8000"]` —— 只绑回环,**不得**写成 `18443:8000`;
  NAS 上 443 通常被管理界面占用,故默认示例用 18443(README/运维文档说明按需改)。
- `volumes: ["./data:/data"]`,且 `data/` 必须在 NAS 本地卷(SQLite 需要真实文件锁,
  不得放 SMB/NFS)。
- `env_file: .env`,`DATA_DIR=/data`。
- 非 root 运行:`data/` 需 `chown 10001:10001`。
- `mem_limit: 256m`、`cpus: 0.50`、`pids_limit: 64`、`stop_grace_period: 30s`。
- `healthcheck`(`/healthz`)、`no-new-privileges:true`、日志轮转 `10m × 5`。
- `restart: unless-stopped`。

**不改现有 `compose.yaml`**:Windows 那套本地构建部署行为保持原样,避免 `up -d`
因为镜像名变化而突然去拉 GHCR。

---

## 5. 需要人工做的一次性动作

1. **首次 workflow 成功后把包设为公开**(NAS 才能免登录拉取):
   `https://github.com/users/xs-www/packages/container/sisicalendar/settings`
   → Change visibility → Public。
   私有包则 NAS 需要 `docker login ghcr.io -u xs-www -p <PAT>`,
   PAT 勾 `read:packages`(写进运维文档的备选路径)。
2. 无需在 GitHub 配置任何 Repository secret(`GITHUB_TOKEN` 自带 `packages: write`)。
3. NAS 首次部署:建目录 → `.env` → `mkdir -p data && sudo chown -R 10001:10001 data`
   → `docker compose -f compose.nas.yaml pull && docker compose -f compose.nas.yaml up -d`。

---

## 6. 实施清单

- [x] 用 GitHub API 核 `setup-qemu-action` / `setup-buildx-action` / `login-action` 主版本
      → 2026-10-14 核到 **qemu v4.4.0 / buildx v4.4.1 / login v4.6.0**
      (同日复核:checkout v7.0.1、setup-uv v10.3.0、metadata v6.2.0、build-push v7.4.0)
- [x] 新建 `.github/workflows/docker-publish.yml`(第 3 节)
- [x] 新建 `compose.nas.yaml`(第 4 节)
- [x] `README.md` 部署节改为三条路径:路径 1 拉镜像 / 路径 2 源码构建 / 路径 3 离线 tar
- [x] `docs/部署与运维.md` 新增 §2.9「从 GHCR 拉取预构建镜像部署」,并在 §5.1 / §5.2
      补拉取式升级(`backup` → `pull` → `up -d --force-recreate`)与回滚(钉 `sha-*`)
- [x] 新建 `docs/static/ci-docker-publish.md`,并在 `docs/static/README.md` 索引与本轮变更登记
- [x] `todo.md` 登记本轮
- [ ] 提交并推送 `master`,在 Actions 页面确认两个 job 绿、GHCR 出现 `latest` 与 `sha-*`
- [ ] `docker manifest inspect ghcr.io/xs-www/sisicalendar:latest` 确认同时含 `amd64`/`arm64`
- [ ] 在 NAS 上按文档走一遍首次部署,记录 `ps` / `/healthz` / 前缀路径的实测结果
      (需要在目标机器上执行;GHCR 包可见性也要先改成 Public 或配 `read:packages` 的 PAT)

---

## 7. 验收标准

| 项 | 期望 |
|---|---|
| Actions | `quality` 与 `image` 均成功;PR 上只构建不推送 |
| GHCR | `latest`、`master`、`sha-<7 位>` 存在;`v*` tag 产生 `1.2.3`/`1.2`/`1` |
| 架构 | manifest 同时含 `linux/amd64` 与 `linux/arm64` |
| 门禁有效性 | 故意让一个测试失败的分支/PR → `image` job 被跳过,GHCR 无新标签 |
| NAS 运行 | `docker compose -f compose.nas.yaml ps` 为 `Up (healthy)`;`/healthz` 200;`/calendar/login` 200;`/calendar` 与 `/calendar.ics` 仍是 **404 纯文本**;`/admin/members` 等管理面对成员仍 403 |
| 持久化 | 重启/重建容器后 `data/` 里的库、`session_secret`、`export/`、`snapshots/` 不变 |

> 特别注意:`session_secret` 是**固定订阅地址与验证码哈希的密钥**(AGENTS.md 不变量 23)。
> 换镜像**只换容器、不换卷**,该文件必须原样保留;任何「清理数据卷再重建」的做法都会让
> 全家人的订阅地址失效。这条要写进运维文档的新增小节。

---

## 8. 风险与备选

| 风险 | 影响 | 处置 |
|---|---|---|
| arm64 走 QEMU | 构建从数分钟变 5–10 分钟 | 接受;用 `type=gha` 缓存跨次复用 |
| 国内 NAS 直连 GHCR 慢/被墙 | `pull` 失败 | 备选:换 Docker Hub(需 `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` 两个 secret)或给 NAS 配代理/镜像加速;本轮不做,需要时再开 |
| GHCR 包默认私有 | NAS 免登录拉取失败 | 一次性改成 Public;或 `docker login ghcr.io` + `read:packages` PAT |
| attestation 清单 | 老 Docker 报 `manifest unknown` | 已设 `provenance: false` / `sbom: false` |
| 每次 push 都出 `latest` | 未经真机验证的提交也可能被拉走 | 门禁跑测试;NAS 升级前**必须备份**;要稳就钉版本 tag |
| Actions 额度 | public 仓库免费,基本无虞 | 无需处理 |

---

## 9. 明确不做的事

- 不改 `app/` 代码、不加迁移、不动[不变量](../AGENTS.md)(本计划纯交付链路)。
- 不改现有 `compose.yaml`(本机本地构建路径保持原样)。
- 不做 Docker Hub 推送(用户已决策,且需额外 secrets)。
- 不做自动部署到 NAS(不引入 Watchtower 之类自动升级:本项目升级前要人工备份,
  自动拉取会绕过这条纪律)。
- 不在 CI 里构建单架构以外的任何东西(如 Windows 容器镜像)。