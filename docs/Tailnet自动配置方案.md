# Tailnet 自动/半自动配置方案(设计稿)

起草:2026-10-14 · 状态:**待决策,未实施**

需求原话:「如何将 tailnet 的配置也能够以某种方式自动或半自动的配置。如项目默认 admin / admin
能够进入管理界面并作为家庭第 0 个成员。登录后允许用户自行根据自己的 tailnet 来配置预期的
baseURL。并自动检测本地 tailscale 环境,且自动进行端口映射。」

## 0. 结论速览

| 子需求 | 能不能自动 | 建议做法 | 代价 |
|---|---|---|---|
| ① 开箱即有管理员可用 | **现在已经基本是** | 保持随机口令打印一次 + 补「首登引导」;若要 `admin/admin`,必须加约束 | 见 §2.1,默认口令 = 把管理员交给整个 tailnet |
| ② 登录后自己配 baseURL | **能,但只能改 origin**(主机名/端口),路径前缀不能运行时改 | 新增可覆盖键 + 设置页卡片 + 「一键采用当前访问地址」 | §2.2,约 1 屏改动 |
| ③ 自动检测 tailscale / 端口映射 | **检测能全自动;改 `tailscale serve` 不能**(除非上 sidecar) | 从请求推导 tailnet 地址 + 容器内探宿主端口 + 生成可复制命令 | §2.3 |

一句话:**「检测」可以全自动,「改整机 serve 配置」只能半自动(给人一条可复制的命令),
要全自动就得引入 Tailscale sidecar 容器**——那不是配置项,是换部署形态。

## 1. 三条硬约束(决定了方案的上限)

### 约束 A:公开面的**路径前缀**只在启动时挂载一次

- `app/main.py:126` 取 `urls.public_base_path(resolved)` 得到 `base_path`,然后
  **同一套路由注册两遍**:裸路径(`app/main.py:144`)+ `prefix=base_path`
  (`app/main.py:155`);中间件在**每个请求**里调用
  `urls.bind_prefix(request.url.path, base_path)`(`app/main.py:161`)绑定当次前缀。
- 全仓库 `public_base_path` 只有两个消费者:`app/main.py:126`(启动挂载)与
  `app/config.py:145`(派生属性)。而 `urls.public_base_url(settings)` 被
  **请求期**到处使用(pages / api_settings / email_verify / sync_service)。
- 推论:**改 origin(协议+主机+端口)可以立刻生效,不需要重启;改路径前缀不行**——
  链接会按新前缀渲染,而路由还挂在旧前缀上,`bind_prefix` 也仍按旧的 `base_path` 判定,
  结果是页面里全是打不开的链接。方案 §2.2 据此把运行时可改范围**限制在 origin**。

### 约束 B:`tailscale serve` 是整机配置,容器内碰不到也不该碰

- 应用跑在非 root 容器里(uid/gid `10001:10001`、`no-new-privileges:true`),
  没有 taiscaled socket、没有特权;
- `tailscale serve` 影响**整台 NAS** 的所有入口(你自己的机器上就同时挂着 `/`、`/dsh`、
  `/agent`、`/calendar`、`/calendar.ics`,见《部署与运维》§3),误操作会删掉别的服务;
- 因此:**容器内的应用永远不会去执行 serve**(§6 红线)。它能做的是「把命令生成好给人复制」。

### 约束 C:权限边界是账号 + 角色,不是网络隔离

- `AGENTS.md` 部署陷阱 3 写得很清楚:Docker NAT 之下本机请求与代理请求同源,
  **不要指望靠客户端地址做管理面隔离**;
- `/calendar/login` 是**故意对 tailnet 开放**的(家人要能自助登录),所以
  「只有 tailnet 能访问」不能当作「默认口令没关系」的理由:tailnet 是家庭设备集合,
  不等于管理员角色集合;共享设备、访客节点、被借用的手机都在这条边界之内。

## 2. 三个子需求分开定级

### 2.1 需求①:默认 `admin/admin` 直接进管理界面

**现状(重要)**:`app/security.py:71` 的 `ensure_admin_credential` 优先级是
`ADMIN_PASSWORD_HASH` > `ADMIN_PASSWORD` > 已有 `data/admin.hash` > **随机生成一个新口令**,
最后一种会在启动日志里打印一次(`app/main.py:66` `_log_credentials`)。

> 也就是说:**今天已经是零配置**——`.env` 里 `ADMIN_PASSWORD` 留空,首次启动把随机口令
> 打印到 `docker logs` 一次,`data/admin.hash` 之后不再变化。
> 「默认 admin/admin」换来的只是「不用去看那一行日志」。

风险清单(为什么我不建议把它做成默认):

1. `/calendar/login` 对整个 tailnet 开放,**默认口令 = 任何人只要猜到两个单词就是管理员**;
   而管理员能改收件人、发群发邮件、停用账号、看全部共享日程。
2. 仓库是 **public**。把「默认 admin/admin」写进 README/`.env.example`,等于公开一份后门配方;
   自动化扫描器确实会扫这类模式,装完被扫到的概率不低。
3. 没有一个**强制改密**的落点的话,`data/admin.hash` 会把弱口令固化下来,
   而它又在 `data/` 卷里,升级镜像也不会覆盖 —— 弱口令会一直留着。
4. 首位管理员豁免邮箱验证(不变量 22,本机救急不能被邮箱锁在门外)在默认口令下
   变成「**任何人**都能用这个豁免」。

四个可选做法:

| 方案 | 便利性 | 风险 | 改动量 |
|---|---|---|---|
| **A0 保持现状**(随机口令打印一次) | 要看一眼 `docker logs` | 无 | 0(已经实现) |
| **A1 一次性引导码**(推荐):首次启动除随机口令外,再写 `data/bootstrap_token`(600),日志打印 `https://<tailnet>/calendar/bootstrap?token=…`;点开后**强制设置管理员口令**,设置成功后立刻删除该文件 | 少一次复制口令 | 无(口令仍由用户当场设定) | 小:1 个路由 + 1 个模板 + 1 个 token 文件 |
| **A2 受限默认口令**(你原话的版本):`ADMIN_ALLOW_DEFAULT=1` 时才启用,且**仅当库里一个账号都没有**;登录后**只能**访问改密页(其余 303),改密后解除;管理页顶部常驻「仍在使用默认口令」高危横幅 | 真正开箱即用 | 在改密前的窗口内,tailnet 上任何人都是管理员;且必须避免写进文档当推荐 | 中:登录守卫 + 强制改密页 + 横幅 + 测试 |
| **A3 首个注册者成为管理员**:库里无账号时,第一个成功注册/登录的人自动提权为 admin(口令自选,无弱口令) | 开箱即用且无弱口令 | 谁先到谁当管理员(等价暴露面,但无「弱口令」这个放大器) | 中:注册路径提权 + 竞态处理 |

**建议**:先做 A1;如果你确实要 A2,请接受「opt-in + 空库限定 + 强制改密 + 常驻横幅」这四条,
并且**不把默认口令写进 README 的推荐路径**(只在设计文档里作为可选项记录)。

### 2.2 需求②:登录后在界面上配 baseURL(推荐 B1)

**B1(推荐,不需要重启)**——只允许改 origin:

- `app/settings_store.py:23` 的 `OVERRIDABLE_KEYS` 增加一键(命名待定,
  建议 `public_base_url`,但语义是「origin 覆盖」);
- `validate_overrides`(`app/settings_store.py:49`)增加分支:用 `urlsplit` 解析,
  要求 `scheme ∈ {http, https}`、有 `netloc`、**没有 query/fragment/userinfo**,
  且**路径部分规范化后必须等于当前挂载前缀**(否则报错并解释「路径前缀只能通过
  `PUBLIC_BASE_URL` 环境变量改,改完需要重建容器」);
- `effective_settings`(`app/settings_store.py:115`)把 origin 与 env 的路径拼成完整 URL
  再 `model_copy` 进 `public_base_url`;env 仍是**唯一**的路径来源;
- 设置页(`/admin/settings`,管理员可见)新增「对外地址」卡片,显示三行:
  - **当前生效**:`https://sisi-laptop.tail921448.ts.net/calendar`
  - **来源**:环境变量 / 界面覆盖
  - **挂载前缀**:`/calendar`(只读,说明「改它要改 `PUBLIC_BASE_URL` 并重建容器」)
  - 按钮:**「采用当前访问地址」**——从本次请求推导(见 §2.3),`*.ts.net` 才允许一键采用
- 影响面(改完立刻生效,无需重启):邀请链接(`pages.py:846`)、每人的固定订阅地址、
  邮件正文里的验证页地址(`email_verify.py:180`)与订阅地址、`/api/v1/settings` 的回显。
- **不改**路由挂载、前缀绑定、订阅地址的路径形态(仍是 `/calendar/<令牌>.ics`)。

**B2(不建议现在做)**:连路径一起改。要么**必须重启容器**,要么引入「前缀剥离中间件」
(与不变量 8「不做前缀剥离」直接冲突,且 `/calendar.ics`、`/calendar` 前缀根那两条
特殊语义都要重新论证)。若将来真要做,推荐做法是「保存后提示『需要重建容器生效』」,
而不是在应用里自杀重启(`restart: unless-stopped` 会把容器拉回来,但启动失败就是重启循环)。

### 2.3 需求③:检测 tailscale 环境 + 自动端口映射

先分清「端口映射」的两层含义,它们的自动化上限完全不同:

| 层 | 是什么 | 谁负责 | 能自动到什么程度 |
|---|---|---|---|
| ① 容器端口发布 | `compose.nas.yaml` 里的 `127.0.0.1:18443:8000` | compose | 应用**改不了**(要重建容器);但可以**自动检测是否真的通** |
| ② serve 路径映射 | `--set-path=/calendar → http://127.0.0.1:18443/calendar` | 宿主机 `tailscale serve` | 应用**不能执行**;只能生成命令(或走 C3) |

**C1(推荐,零特权,分三段)**:

- **(a) 从请求推导 tailnet 地址**:管理员用 `https://<node>.<tailnet>.ts.net/calendar/admin/settings`
  打开设置页时,请求里就带着 `Host` 和(经 serve 转发时)`X-Forwarded-Proto`。
  用它们拼出候选 origin,只把 **`.ts.net` 结尾**的候选做成「一键采用」,
  其余只展示、要求手动确认。
  > Tailscale serve 会追加转发头(其 PR 标题即
  > [serve/funnel add forwarded host and proto header](https://github.com/tailscale/tailscale/pull/8224));
  > **确切头名与是否始终存在,实施时用一次真机 `curl -v` 记录为准**(见 §5)。
  > 注意:这两个头是**建议来源,不是安全判据**——只有管理员确认后才写库。
- **(b) 自动检测宿主端口映射**(这一条是真正的「自动检测」):
  `compose.nas.yaml` 增加 `extra_hosts: ["host.docker.internal:host-gateway"]`
  与 `environment: HOST_PORT=${HOST_PORT:-18443}`;设置页放一个
  「检测宿主端口」按钮,应用从容器内请求
  `http://host.docker.internal:<HOST_PORT>/healthz`(2 秒超时),把结果显示成
  「宿主 `127.0.0.1:18443 → 容器 8000`:✅ 可达 / ❌ 不可达(检查 compose 的 `ports`)」。
  > 安全:**host 固定写死 `host.docker.internal`,端口只接受 1–65535 的整数**,
  > 绝不接受任意 URL —— 否则这个按钮就成了 SSRF 工具。
- **(c) 生成可复制的 serve 命令**(把 ① 的端口与 ② 的路径都填进去):
  ```bash
  # 1) 先快照(《部署与运维》§3 警告一:serve 是整机配置)
  tailscale serve status | tee tailscale-serve-backup.txt
  # 2) 只加 /calendar 一条,不动其他入口
  tailscale serve --bg --yes --https=443 --set-path=/calendar http://127.0.0.1:18443/calendar
  ```
  注意现有文档里的例子是 `http://127.0.0.1:443`(`compose.yaml` 映射 443),
  而 `compose.nas.yaml` 映射 **18443**,所以命令必须由 `HOST_PORT` 生成,不能照抄。

**C2(半自动,一条命令)**:仓库加 `scripts/tailscale-serve.ps1` / `.sh`,在**宿主机**上跑:
读 `tailscale status --json` 拿 `Self.DNSName` → 打印快照命令与 serve 命令 →
只有显式 `-Apply` / `--apply` 才真的执行。这样「检测本地 tailscale 环境」是自动的,
「改配置」仍由人按下回车。

**C3(全自动,进阶/可选)**:Tailscale 官方容器做 sidecar(与应用共享网络命名空间),
`TS_AUTHKEY` 入网,`TS_SERVE_CONFIG=/data/serve.json` 读我们生成的 serve 配置 ——
应用只要往 `/data` 写 `serve.json`,serve 配置就跟着变,真正「点一下生效」。
代价:多一个 tailnet 节点、需要一枚 auth key(敏感值)、tailnet 要在管理台
**启用 HTTPS 证书**([TS_SERVE_CONFIG is ignored when HTTPS is not enabled](https://github.com/tailscale/tailscale/issues/14495)),
compose 形态与排障复杂度都会上一档。**不建议作为默认路径**,可作为 `compose.tailscale.yaml` 的可选 profile。

## 3. 分期实施计划(每期可独立验收)

### 第 1 期(推荐先做):B1 + C1 —— 「自己配地址 + 自动检测端口」

- 改动:`app/settings_store.py`(可覆盖键 + 校验)、`app/routers/pages.py`(设置卡片)、
  `app/templates/`(设置页片段,无内联脚本,值走 `data-*`)、`compose.nas.yaml`
  (`extra_hosts` + `HOST_PORT`)、`.env.example`(注释说明)。
- 新增测试:`tests/test_settings_public_base_url.py`:
  只改 origin 生效;带路径/带 query/带 userinfo → 422;非法 scheme → 422;
  **env 的路径仍是唯一来源**(覆盖前后 `public_base_path` 不变);
  推导 `Host` 的候选只在 `*.ts.net` 时给出;`HOST_PORT` 非数字被拒。
- 验收:`/admin/settings` 改 origin → 「我的设置」里的固定订阅地址与邀请链接立刻跟着变;
  改路径 → 表单报错且链接不变;点检测 → 显示宿主端口可达性。
- 不变量影响:8(前缀来源唯一)保持;新增一条「origin 可被库覆盖,路径不可」。

### 第 2 期:C2 —— 宿主机脚本

- 改动:`scripts/tailscale-serve.ps1` / `.sh`(默认 dry-run)、`docs/部署与运维.md` §3 补一节。
- 验收:在**已有多入口**的机器上跑 dry-run,输出与 `tailscale serve status` 一致;
  `-Apply` 后 `/calendar` 200、`/calendar` 仍 404 纯文本、其他入口(`/`、`/dsh`)未被删除。

### 第 3 期(可选):A1 首登引导(或 A2/A3,按 §6 的决定)

### 第 4 期(可选,不建议默认):C3 sidecar

## 4. 待实测确认(实施前先各测一次,别写进文档当事实)

1. serve 转发到后端的请求里,到底带哪些头(`X-Forwarded-Proto` / `X-Forwarded-Host` / `Host`),
   以及 HTTP/2 下是否一致 —— 在真机上 `curl -v` 一次即可确认。
2. NAS 的 Docker 版本是否支持 `host.docker.internal:host-gateway`(Docker 20.10+);
   不支持则 (b) 段退化为「只能手动确认端口」。
3. `TS_SERVE_CONFIG` 在目标 tailnet 上是否已启用 HTTPS 证书。

## 5. 明确不做的事(红线)

- **不把 `/var/run/tailscale/tailscaled.sock` 挂进应用容器**。技术上它能让应用直接驱动
  `tailscale serve`,但:(1) 该 socket 需要 root 或 `tailscale set --operator`,与
  「非 root + `no-new-privileges`」直接冲突;(2) 等于把整机的 tailnet 暴露面交给应用进程,
  一旦应用有 RCE,攻击者就能改整台 NAS 的入网入口。这属于「便利换掉安全边界」,不做。
- **不在应用里执行 `tailscale serve` / `tailscale serve reset`**。
- **不把默认 `admin/admin` 写进 README 的推荐路径**(公开仓库 = 公开后门配方)。
- **不让 origin 覆盖影响路径前缀**(§2.2 B1 的校验就是为此)。
- **不把「检测宿主端口」做成任意 URL 探测器**(host 写死、端口限数字)。

## 6. 需要你决策的三件事

1. **需求①走哪条**?A0 保持现状 / **A1 一次性引导码(推荐)** / A2 受限默认 `admin/admin` /
   A3 首个注册者成为管理员。
2. **需求②的范围**:**B1 只改 origin(推荐,免重启)** / B2 连路径一起改(需要重建容器)。
3. **需求③做到哪一档**:**C1 应用内检测 + 复制命令(推荐)** / C1+C2 再加宿主机脚本 /
   一直做到 C3 sidecar(全自动,换部署形态)。

## 变更记录

- 2026-10-14:新增本文档(设计稿,未实施)。结论:origin 可运行时覆盖、路径前缀不可;
  serve 只能半自动;默认口令不建议作为默认路径,给出 A1/A2/A3 三个替代。