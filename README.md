# SisiCalendar

单用户、轻量、私有的本地日程与订阅日历服务。

- 浏览器访问 `http://localhost:443/admin` 编辑日程(仅本机可访问)。
- SQLite 是日程数据的唯一事实来源,`calendar.ics` 是由数据库派生的只读发布文件。
- iPhone 通过 Tailscale Serve 提供的 HTTPS 地址订阅只读日历。
- 每次确认保存并成功生成新版 ICS 后,向启用的同步人员邮件分发**完整**日历快照。

设计依据见 [`docs/本地日程与订阅日历服务-开发文档.md`](docs/本地日程与订阅日历服务-开发文档.md)。

## 功能一览

| 模块 | 能力 |
|---|---|
| 日程管理 | 月/周/日/列表视图,普通与全天事件,标题、地点、说明、状态、IANA 时区、重复规则与截止日期 |
| 并发安全 | 基于 `updated_at` 的乐观锁,旧页面提交返回 409 而不是静默覆盖 |
| ICS 发布 | RFC 5545 完整导出,UTC/`TZID`+`VTIMEZONE`,稳定 `UID`,递增 `SEQUENCE`,原子替换 |
| 订阅接口 | `/calendar` 与 `/calendar.ics`,ETag、Last-Modified、`304`、HEAD,不做 HTML 重定向 |
| 失败韧性 | 导出失败保留编辑、标记 `dirty`、每分钟自动修复,订阅端始终返回最近的有效文件 |
| 邮件同步 | 持久化队列、`(版本, 收件人)` 幂等、逐人独立发送、退避重试、旧版附件隔离 |
| 安全 | Argon2id 口令、签名会话、CSRF 双提交、限流、严格 CSP、口令只从环境注入 |
| 运维 | 健康/就绪检查、一致性自检、在线备份与恢复、Alembic 迁移、Docker Compose |
| 远程 Agent | 可选的 Bearer 令牌接口,供外部 Agent 经 HTTP 新建日程;默认关闭,幂等键防重试重复 |

## 远程 Agent 接入(默认关闭)

给本机之外的脚本或 Agent 一个写入入口。未配置 `AGENT_API_TOKEN` 时接口返回 404,等同不存在。

```powershell
curl.exe -X POST https://sisi-laptop.tail921448.ts.net/agent/events `
  -H "Authorization: Bearer <AGENT_API_TOKEN>" `
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

带同一个 `client_ref` 重试是安全的:服务端返回已存在的事件(`"duplicate": true`),
不会重复建事件,也不会重复发信。请求体也能用表单形式,方便 iOS 快捷指令直接调用:

```text
POST https://sisi-laptop.tail921448.ts.net/agent/events
Authorization: Bearer <AGENT_API_TOKEN>
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

打开 `http://127.0.0.1:8443/admin`;订阅地址为 `http://127.0.0.1:8443/calendar`。
数据库迁移在应用启动时自动执行,数据默认放在仓库下的 `data/`(可用 `DATA_DIR` 改)。

## 测试

```powershell
uv run pytest -q                 # 89 项测试
uv run ruff check app tests      # 静态检查
```

覆盖 ICS 语义与时区/夏令时、事件 API 与并发冲突、订阅条件请求、原子替换与失败回退、
邮件幂等与退避重试、备份恢复往返。验收标准逐条对照见
[`docs/验收测试记录.md`](docs/验收测试记录.md)。

## 部署

```powershell
Copy-Item .env.example .env      # 填写 SMTP、时区、订阅地址
docker compose up -d --build
```

管理地址 `http://localhost:443/admin`,只绑定 `127.0.0.1`。
订阅入口通过 Tailscale Serve 单独暴露:

```powershell
tailscale serve status          # 先留存现状
tailscale serve --bg --yes --https=443 --set-path=/calendar http://127.0.0.1:443/calendar
```

> 目标地址里必须带上 `/calendar`。Tailscale Serve 会剥掉挂载前缀,若写成
> `http://127.0.0.1:443`,则 `/calendar/admin` 会落到后端的管理首页,把管理界面暴露给整个 tailnet。

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
