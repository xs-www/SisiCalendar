# `.github/workflows/docker-publish.yml` + `compose.nas.yaml`(交付链路契约)

本文件记录「提交 → 门禁 → 双架构镜像 → GHCR → NAS 拉取」这条链路的**契约**:
触发条件、标签规则、平台、权限与不可改动的约束。改动这条链路前先看这里,
设计与验收标准见 [`../CI镜像自动发布计划.md`](../CI镜像自动发布计划.md),
运维步骤见 [`../部署与运维.md`](../部署与运维.md) §2.9 / §5.1 / §5.2。

它不产出应用内函数,所以下面按「产物 + 契约字段」而不是按函数签名记录。

---

## `docker-publish.yml`(GitHub Actions)

- **位置**:`.github/workflows/docker-publish.yml`
- **用途**:`master` 每次推送后跑门禁,再构建 `linux/amd64` + `linux/arm64` 镜像并推送到 GHCR
- **触发**:

  | 事件 | 行为 |
  |---|---|
  | `push` 到 `master` | 门禁 + 构建 + 推送(`latest` / `master` / `sha-<7位>`) |
  | `push` tag `v*` | 门禁 + 构建 + 推送(`1.2.3` / `1.2` / `1` / `sha-<7位>`) |
  | `pull_request` → `master` | 门禁 + **只构建、不推送**(验证 Dockerfile 没被改坏) |
  | `workflow_dispatch` | 同 `master`(手动补一次) |

- **并发**:`group: docker-${{ github.ref }}` + `cancel-in-progress: true`
  ——同一 ref 连续提交只跑最后一次;跨分支互不干扰
- **权限**:顶层 `contents: read`;`packages: write` 只给 `image` job
  ——**不需要任何自定义 secret**,登录用自带的 `secrets.GITHUB_TOKEN`
- **job `quality`(门禁)**:`uv sync --frozen` → `uv run ruff check app tests migrations`
  → `uv run pytest -q`;`timeout-minutes: 20`
- **job `image`**:`needs: quality` → QEMU → Buildx → 登录 GHCR → metadata → build-push;
  `timeout-minutes: 45`
- **平台**:`linux/amd64,linux/arm64`(arm64 走 QEMU,构建明显慢于 amd64)
- **缓存**:`cache-from/to: type=gha,mode=max`
- **返回**:无(流水线产物是 GHCR 里的镜像与 OCI 标签)
- **副作用**:向 `ghcr.io/xs-www/sisicalendar` 推送镜像;PR 上无副作用
- **不变量**:
  1. **门禁是硬前置**:`quality` 失败时 `image` 不执行,坏提交永远不会变成 `latest`
     ——这是本链路唯一防止「未通过测试的代码上机」的机制,不要改成 `continue-on-error`。
  2. **镜像名必须小写**:`ghcr.io/xs-www/sisicalendar` 写死在 `env.IMAGE_NAME`,
     不依赖 `metadata-action` 的自动转换(GHCR 拒绝大写仓库名)。
  3. **`provenance: false` / `sbom: false` 必须保留**:attestation 清单会让部分老 NAS 的
     Docker 报 `manifest unknown`。打开它们等于放弃对这些设备拉取的支持。
  4. **Action 版本**:`checkout@v7`、`setup-qemu@v4`、`setup-buildx@v4`、`login@v4`、
     `metadata@v6`、`build-push@v7` 用浮动主版本标签(2026-10-14 逐个用
     `GET /repos/{owner}/{repo}/git/ref/tags/{tag}` 验过存在);
     **`astral-sh/setup-uv` 必须钉精确版本 `@v10.3.0`** —— 该仓库不发布浮动主版本标签
     (`v10` 不存在,最高浮动标签只到 `v7.6`),写成 `@v10` 会让 job 在 `Set up job` 阶段直接失败
     (`Unable to resolve action astral-sh/setup-uv@v10, unable to find version v10`)。
     它的 release 标了 `"immutable": true`,所以钉精确版本是稳定且更安全的做法。
  5. **`latest` 只跟默认分支**:推送 tag 不移动 `latest`;要发布版就显式钉版本号。
  6. 镜像里**不得**出现 `data/`、`.env`、`*.db`:由 `.dockerignore` 保证,改动它要重新确认。
- **测试**:无自动化测试(工作流本身靠首次运行与 PR 构建验证);
  门禁效果按计划 §7「门禁有效性」人工验证一次即可
- **变更记录**:2026-10-14 新建(门禁 + 双架构 + GHCR)

---

## `compose.nas.yaml`(NAS 拉取式部署)

- **位置**:`compose.nas.yaml`
- **用途**:在不需要代码仓库与构建工具的机器上,从 GHCR 拉取镜像并运行
- **参数**:无(读同目录 `.env`;`env_file: .env`)
- **返回**:无
- **副作用**:创建/替换容器 `sisi-calendar`;读写宿主 `./data`
- **不变量**(与 `compose.yaml` **逐字对齐**,只有 `build:` → `image:` 这一处不同):
  1. **端口只绑回环**:`"127.0.0.1:18443:8000"`。省略 `127.0.0.1` 会让 Docker 绑定
     `0.0.0.0`,把管理面暴露到整个局域网——这是安全边界,不是可选项。
     NAS 上 443 常被管理界面占用,故默认 18443(宿主端口可按需改,回环地址不能丢)。
  2. **`data/` 用宿主绑定挂载且必须在本地卷**:`./data:/data`。SQLite 需要真实文件锁,
     不得放在 SMB/NFS 上。
  3. **非 root 运行**:镜像以 `10001:10001` 运行,首次启动前 `chown -R 10001:10001 data`。
  4. **资源与回收约束不得放宽**:`mem_limit: 256m`、`cpus: 0.50`、`pids_limit: 64`、
     `stop_grace_period: 30s`、`no-new-privileges:true`、日志 `10m × 5`、`restart: unless-stopped`。
  5. **健康检查固定打 `/healthz`**:与 Dockerfile 的 `HEALTHCHECK` 一致。
  6. **服务名保持 `calendar`**:《部署与运维》里所有 `docker compose exec calendar …`
     都依赖它;改名会让文档里的运维命令全部失效。
  7. **`latest` 可被钉版本**:升级用 `latest`,回滚把 `image:` 改成 `:sha-<7位>` 或 `:1.2.3`。
  8. **不要改 `compose.yaml` 去引用 GHCR**:本机(Windows)那条路径保持本地构建,
     避免 `up -d` 因为镜像名变化而突然去拉远程镜像。
  9. **`HOST_PORT` 与 `extra_hosts` 只服务设置页的「对外地址(Tailscale)」卡片**:
     `environment.HOST_PORT: "${HOST_PORT:-18443}"`(`compose.nas.yaml:40`)让页面生成的
     `tailscale serve` 命令端口正确(**不能照抄 443**:NAS 默认发布的是 18443),
     `extra_hosts: ["host.docker.internal:host-gateway"]`(`:44`–`:45`)让容器内能探测
     宿主端口映射是否真的通了(**管理员点一下才跑**,见
     [`app-host-port.md`](app-host-port.md))。`.env` 没给 `HOST_PORT` 时用 compose
     默认值 18443,与第 1 条的 `ports` 默认一致 —— **改 `ports` 的宿主端口时
     要同时改 `HOST_PORT`**,否则设置页给出的命令会指向一个没人监听的端口。
     这两项**不改变监听**:容器里永远是 `PORT=8000`,探测的主机名写死、端口只认整数。
    `.env.example:41` 同样给了 `HOST_PORT`(默认 `443`,与 `compose.yaml` 一致;
    用 `compose.nas.yaml` 时改成 18443)并写明「改了宿主的 `ports` 就要同步改这里」。
- **测试**:无自动化测试;首次 NAS 部署按《部署与运维》§2.9 的清单人工验证
- **变更记录**:2026-10-14 新建;同日补充第 9 条(`HOST_PORT` 与 `extra_hosts`)

---

## 与其它文档的分工

- 设计与验收标准、风险与备选:[`../CI镜像自动发布计划.md`](../CI镜像自动发布计划.md)
- 首次部署 / 升级 / 回滚的操作步骤:[`../部署与运维.md`](../部署与运维.md) §2.9、§5.1、§5.2
- 面向 Agent 的最短路径与硬约束:`README.md`「部署」节
- 宿主端口探测与「对外地址」卡片:[`app-host-port.md`](app-host-port.md)、
  [`app-routers-pages.md`](app-routers-pages.md)

## 变更记录

- 2026-10-14:新建本文件(门禁 + 双架构镜像 + GHCR 拉取部署契约)。
- 2026-10-14:`compose.nas.yaml` 的 `environment` 增加
  `HOST_PORT: "${HOST_PORT:-18443}"`(`:40`),并新增
  `extra_hosts: ["host.docker.internal:host-gateway"]`(`:44`–`:45`)——这两项**不改变监听**
  (容器内永远是 `PORT=8000`),只服务设置页的「对外地址(Tailscale)」卡片:
  `HOST_PORT` 决定生成的 `tailscale serve` 命令端口(NAS 上不能照抄 443),
  `extra_hosts` 让容器能探测宿主端口映射(管理员点击才跑,只连
  `host.docker.internal:<HOST_PORT>/healthz`,见 [`app-host-port.md`](app-host-port.md));
  改 `ports` 的宿主端口时必须同步改 `HOST_PORT`。