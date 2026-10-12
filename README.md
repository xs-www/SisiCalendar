# SisiCalendar

以家庭为单位的多人共享日历:3–5 个账号,每人一条独立的私人日历,外加一条全家共享的日历。
轻量、私有、本机自托管。

- 管理员在本机用 `http://localhost:443/admin` 编辑日程、管成员与设置(裸路径只在本机可用)。
- 家庭成员从公开面登录:`https://sisi-laptop.tail921448.ts.net/calendar/login`。
- SQLite 是日程数据的唯一事实来源,`calendar.ics` 是由数据库派生的只读内部产物。
- **私人日历只有本人能看到**(连家庭管理员也读不到),**家庭共享日历**全家可见。
- **每人一条固定的订阅地址** `https://<节点>/calendar/<自己的令牌>.ics`,iPhone 订阅的是
  「个人 + 家庭共享」的合并结果;地址由服务端派生、永不变更,在「我的设置」里可以随时复制,
  验证邮箱后收到的那封邮件里也写着它。
- 每次确认保存并成功生成新版 ICS 后,按收件人的范围分别邮件分发**尚未结束的日程**快照:
  账号收件人收到自己可见的日历与**他自己的**订阅地址,外部邮箱只收到家庭共享日历、不给任何地址。
- **旧的订阅地址 `/calendar`(无令牌)已经失效**:手机里存过它的,请换成自己那条
  `/calendar/<自己的令牌>.ics`。

设计依据见 [`docs/本地日程与订阅日历服务-开发文档.md`](docs/本地日程与订阅日历服务-开发文档.md)。

## 功能一览

| 模块 | 能力 |
|---|---|
| 家庭账号 | 3–5 个账号自助注册(邀请码)/管理员创建;`admin`(家长)与 `member` 两种角色;可停用、重置口令、改角色、删除;停用后不能登录、令牌失效、不再收邮件 |
| 邮箱验证 | **注册必须填邮箱**;系统发 6 位验证码(30 分钟有效、最多错 5 次、重发冷却 60 秒),验证通过后才能使用系统;通过后会收到一封写明**本人固定订阅地址**的邮件;改邮箱立刻打回待验证。**首位管理员豁免**(本机救急用),没填邮箱的老账号只提示、不拦 |
| 隐私 | 私人日历仅归属账号可读可写,他人(含管理员)读取 404、写入 403;越界事件在列表里根本不出现 |
| 共享日历 | 管理员创建多条共享日历;成员默认只读(`viewer`),可被提升为可编辑(`editor`),家长为 `owner`;只有管理员能调整权限 |
| 日程管理 | 月/周/日/列表视图,多日历侧栏勾选与着色,普通与全天事件,标题、地点、说明、链接、状态、IANA 时区(可填序号/中文名)、重复规则与截止日期 |
| 提醒 | 每个事件可配多条相对提醒(开始时/提前 N 分钟/天,全天事件以 00:00 为基准),导出为 ICS `VALARM`;接口与 iOS 快捷指令同名字段可用 |
| 并发安全 | 基于 `updated_at` 的乐观锁,旧页面提交返回 409 而不是静默覆盖 |
| ICS 发布 | RFC 5545 完整导出,UTC/`TZID`+`VTIMEZONE`,稳定 `UID`,递增 `SEQUENCE`,原子替换 |
| 订阅接口 | 公开面统一挂在 `PUBLIC_BASE_URL` 的路径前缀下(默认 `/calendar`);**每人一条固定地址** `/calendar/<自己的令牌>.ics`(可见范围合并),家庭订阅令牌只含共享日历;**旧的无令牌 `/calendar` 与 `/calendar.ics` 已取消,一律 404 纯文本**;ETag、Last-Modified、`304`、HEAD |
| 订阅令牌 | 只存 `sha256`;随机令牌的明文只在创建时显示一次,可停用/撤销,撤销后地址立刻 404;**账号的固定地址由服务端密钥派生、永不变更,界面不给停用/撤销(409),但可以随时重新显示** |
| 失败韧性 | 导出失败保留编辑、标记 `dirty`、每分钟自动修复,订阅端始终返回最近的有效文件 |
| 邮件同步 | 持久化队列、`(版本, 收件人)` 幂等、逐人独立发送、退避重试、旧版附件隔离,队列显示操作/发送时间;**按收件人范围出附件**(账号=本人可见日历,外部=家庭共享),附件只含尚未结束的日程 |
| 安全 | Argon2id 口令、签名会话、CSRF 双提交、注册限流、严格 CSP、口令与 SMTP 密码只从环境注入 |
| 运维 | 健康/就绪检查、一致性自检、在线备份与恢复、Alembic 迁移、Docker Compose |
| 远程 Agent | 可选的 Bearer 令牌接口,供外部 Agent 经 HTTP 新建日程;每枚令牌绑定一个账号与一条默认日历;默认关闭,幂等键防重试重复 |

### 谁能看到什么

| | 自己的私人日历 | 别人的私人日历 | 家庭共享日历 |
|---|---|---|---|
| 成员(`member`) | 读 + 写 | 看不到(404 / 403) | 读;写需被提升为 `editor` |
| 家长(`admin`) | 读 + 写 | 看不到(404 / 403) | 读 + 写(创建共享日历、调权限也仅限家长) |

管理员额外负责:成员与邀请码、应用设置、同步与手动发信、外部收件人。管理员不能停用或删除
自己,系统也不会允许删掉最后一个管理员。

### 时区怎么写

`/admin/settings` 的默认时区是按地区分组的 30 项下拉框,每项带固定序号;事件与 Agent 接口的
`timezone` 字段可以填 IANA 名、中文名或这个序号:

```json
{ "title": "和牙医的预约", "start_at": "2026-11-03T14:00", "timezone": 1 }
```

`1` = 北京时间、`11` = 悉尼时间、`12` = 布里斯班时间。序号只会在表尾追加,已发布的编号不会变。
完整清单见 [`docs/static/app-timezones.md`](docs/static/app-timezones.md),
接口形式见 `GET /api/v1/settings` 的 `timezone_choices`。

## 成员怎么加入

管理员的「成员」页生成邀请码(可设使用次数与有效期,明文只显示一次),页面给出的就是
**可以直接复制粘贴的完整地址**:

```text
https://sisi-laptop.tail921448.ts.net/calendar/register?code=<邀请码>
```

(本机开发时把 `PUBLIC_BASE_URL` 配成 `http://127.0.0.1:8443/calendar`,就是同一个前缀下的地址。)
成员打开它自助注册并登录。**注册必须填邮箱**:系统会发一封 6 位验证码的邮件,验证通过后才能
使用系统;通过后会再收到一封写着**本人固定订阅地址**的邮件。之后:

- 成员的订阅地址在「我的设置 → 我的订阅地址」里长期可见,直接复制到 iPhone 的「添加订阅日历」;
- 成员在「我的设置」里维护邮箱 → 成为**账号收件人**,只收自己可见日历的快照 —— 邮箱通过验证后
  这个收件人才会建立;
- 管理员在「设置」页添加的**外部收件人**(如长辈的邮箱)只会收到家庭共享日历,不含任何人的私人
  日程,也**不会**拿到任何人的订阅地址。

## 从单用户版升级

升级只多一步结构迁移:`uv run alembic upgrade head`(Docker 部署重建镜像后启动时自动迁移)。
首次启动会幂等地补齐家庭版数据:创建首位管理员(`ADMIN_USERNAME`/`ADMIN_PASSWORD`)、他的私人
日历、名为「家庭共享日历」的共享日历,并把**原有日程全部归入首位管理员的私人日历**;历史收件人
与历史 Agent 令牌也绑到他名下(收件人绑的是**收信范围**,不是账号身份,见下),因此**收信范围**
与升级前一致。

> **注意:订阅地址在升级后要换一次。** 旧的无令牌地址 `/calendar` 与 `/calendar.ics` 已不再提供
> 日历(返回 404 纯文本),因为旧行为是把首位管理员的可见范围给任何拿到地址的人。升级后请让每个
> 人在「我的设置」里复制**自己**那条固定地址 `/calendar/<自己的令牌>.ics`,重新添加到 iPhone。

收件人有「身份」与「范围」两件事:`user_id` 非空表示这行是该账号的账号收件人(每个账号一行),
`scope_user_id` 决定这封邮件按谁的可见日历裁剪。单用户版升级上来的历史邮箱保留 `user_id` 为空、
`scope_user_id` = 首位管理员,所以它们收到的日历范围与升级前一模一样,但**不会**收到任何订阅地址
(地址只发给账号本人)。

## 远程 Agent 接入(默认关闭)

给本机之外的脚本或 Agent 一个写入入口。**两个来源都没有令牌时**(`.env` 的 `AGENT_API_TOKEN`
为空且平台内没有启用的令牌)接口返回 404,等同不存在。

令牌可以在 **设置 → 远程接入令牌** 里创建和管理:选择**归属账号**与**默认日历** → 生成 →
页面显示**一次性明文**(复制保存,之后只能看到指纹)→ 可随时改名、停用、撤销。Agent 以该账号的
权限落库:成员的令牌写不进共享日历,除非家长把他提升为 `editor`。数据库里只保存
`sha256(token)` 哈希,明文不进库、不进日志;`.env` 里的 `AGENT_API_TOKEN` 仍然生效,
作为兜底与恢复通道(归属首位管理员)。

```powershell
curl.exe -X POST https://sisi-laptop.tail921448.ts.net/agent/events `
  -H "Authorization: Bearer <令牌>" `
  -H "Content-Type: application/json; charset=utf-8" `
  --data-binary "@event.json"
```

```json
{
  "title": "和牙医的预约",
  "start_at": "2026-11-03T14:00:00",
  "end_at": "2026-11-03T15:00:00",
  "timezone": "Australia/Brisbane",
  "client_ref": "dentist-2026-11-03"
}
```

带同一个 `client_ref` 重试是安全的:同一个账号会拿回已存在的事件(`"duplicate": true`),
不会重复建事件,也不会重复发信;换成别的账号使用同一个 `client_ref` 会被拒绝(409),避免
借用别人的幂等键去探测事件。请求体也能用表单形式,方便 iOS 快捷指令直接调用:

```text
POST https://sisi-laptop.tail921448.ts.net/agent/events
Authorization: Bearer <令牌>
Content-Type: application/x-www-form-urlencoded

title=和客户喝咖啡&start_at=2026-11-06 14:00&duration_minutes=90
```

字段清单、状态码、开启步骤与 iOS 快捷指令配置见
[`docs/部署与运维.md`](docs/部署与运维.md) 第 3.4 节。

## 本机开发

```powershell
uv sync                                         # 创建 .venv 并安装依赖(Python 3.13)
uv run python -m app.cli credential             # 首次会生成管理员口令并打印一次
uv run uvicorn app.main:app --reload --port 8443
```

打开 `http://127.0.0.1:8443/admin`(本机裸路径,一直可用)。
数据库迁移在应用启动时自动执行,数据默认放在仓库下的 `data/`(可用 `DATA_DIR` 改)。

想把家庭成员的界面也在本机试一遍,把 `.env` 里的公开基地址配成本地前缀即可:

```dotenv
PUBLIC_BASE_URL=http://127.0.0.1:8443/calendar
```

之后 `/calendar/login`、`/calendar/register`、`/calendar/admin` 都会可用,每人的订阅地址是
`http://127.0.0.1:8443/calendar/<自己的令牌>.ics`;旧的无令牌 `/calendar` 一律 404。

## 测试

```powershell
uv run pytest -p no:randomly -q  # 422 项测试
uv run ruff check app tests      # 静态检查
```

覆盖 ICS 语义与时区/夏令时、事件 API 与并发冲突、订阅条件请求、原子替换与失败回退、
邮件幂等与退避重试、邮件附件只含尚未结束的日程、时区序号解析、提醒(`VALARM`)取值与确定性导出、
备份恢复往返;**公开面地址前缀与固定订阅地址、邮箱验证全流程、CLI 的 `DATA_DIR` 误指安全网**;
家庭版另有账号/日历/成员权限、可见性与越界 404/403、邀请码注册、
订阅令牌与家庭订阅范围、按收件人范围出附件、Agent 令牌作用域等测试。
验收标准逐条对照见 [`docs/验收测试记录.md`](docs/验收测试记录.md)。

## 部署

```powershell
Copy-Item .env.example .env      # 填写 SMTP、时区与 PUBLIC_BASE_URL
docker compose up -d --build
```

管理面只绑定 `127.0.0.1`(本机 `http://localhost:443/admin`);家庭成员走 Tailscale Serve 上的
**唯一一段 `/calendar`**,它同时承载登录、注册、管理界面与订阅地址:

```powershell
tailscale serve status          # 先留存现状
tailscale serve --bg --yes --https=443 --set-path=/calendar http://127.0.0.1:443/calendar
```

> `PUBLIC_BASE_URL` 的**路径部分就是这段前缀**(默认
> `https://sisi-laptop.tail921448.ts.net/calendar`),应用里的链接按当次请求的前缀渲染:
> 从 `/calendar` 进来页面里全是 `/calendar/...`,从本机 `http://localhost:443/admin` 进来仍是裸路径。
> 挂载路径必须与后端路径**逐字一致**:本仓库的 serve 用法**不剥前缀**,
> `--set-path=/calendar` 配 `http://127.0.0.1:443/calendar` 时,外部的 `/calendar` 与后端的
> `/calendar` 是同一个入口。**不要再挂裸 `/login`、`/admin`、`/api`、`/static`**——那会把管理面
> 暴露到整个 tailnet。
> 改完确认:`/calendar/login`、`/calendar/register`、`/calendar/static/css/app.css` 返回 200,
> `/calendar/admin` 未登录 303,`/calendar/api/v1/events` 匿名 401(应用自己的 JSON),而
> `/calendar`、`/calendar/`、`/calendar.ics` 都是 **404 纯文本**(订阅命名空间没被管理页污染,
> 旧的无令牌地址也确实取消了)。会话密钥请留空交给 `${DATA_DIR}/session_secret` 生成,不要用短口令;
> 注意**轮换该密钥会让所有固定订阅地址与验证码哈希一起失效**。

完整步骤、Tailscale ACL、备份恢复、升级回滚与故障排查见
[`docs/部署与运维.md`](docs/部署与运维.md)。

## 目录结构

```text
app/            应用代码(见 AGENTS.md 的代码地图)
migrations/     Alembic 迁移
templates/      Jinja2 模板(含 HTMX 局部片段)
static/         样式、脚本与本地化前端依赖(vendored,不依赖 CDN)
tests/          pytest 测试
data/           运行时数据,不入版本库
docs/           设计文档、部署运维、验收记录
```

## 许可

私有项目,未公开发布。