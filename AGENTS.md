# AGENTS.md

面向在本仓库继续开发的自动化代理与人类维护者。

## 项目一句话

单用户私有日程服务:SQLite 是唯一事实来源,派生出只读 `calendar.ics` 供 iPhone 订阅,
每次确认保存后向启用的同步人员邮件分发完整日历快照。

设计文档:`docs/本地日程与订阅日历服务-开发文档.md`(需求与验收的最终依据)。
部署运维:`docs/部署与运维.md`。验收证据:`docs/验收测试记录.md`。

## 常用命令

```powershell
uv sync                                   # 安装依赖(需要 uv)
uv run alembic upgrade head               # 手动迁移:url 写死在 alembic.ini,固定 ./data/calendar.db
                                          # 它**不读 DATA_DIR**,别在测试副本上用它(会打到真实库)
uv run uvicorn app.main:app --reload      # 本机开发
uv run pytest -q                          # 全量测试
uv run ruff check app tests --fix         # 静态检查
uv run python -m app.cli verify           # 一致性检查 + 自动修复
uv run python -m app.cli backup           # 生成一致的 SQLite 快照
```

`DATA_DIR` 默认是仓库下的 `data/`,开发时可用环境变量指向别处。测试全部使用临时目录。

## 代码地图

```text
app/
├─ main.py          应用装配、lifespan、安全响应头、异常页
├─ config.py        环境配置与派生路径(唯一配置入口)
├─ db.py            引擎、会话、SQLite PRAGMA(WAL)
├─ models.py        全部表定义
├─ schemas.py       请求/响应模型
├─ deps.py          模板环境、登录依赖、CSRF 依赖
├─ security.py      Argon2id 口令、签名会话、CSRF、限流、审计日志
├─ errors.py        统一 {"error": {...}} 响应
├─ timeutil.py      时间换算(库内一律 naive UTC)
├─ recurrence.py    RRULE 解析/构造/表单互转
├─ events.py        事件校验、查询、重复事件展开、序列化
├─ ics.py           RFC 5545 导出
├─ exporter.py      原子替换、版本状态、快照、一致性检查
├─ sync_service.py  「保存 → 生成 → 建邮件任务」编排(publish 是唯一入口)
├─ mailer.py        SMTP 连接与 MIME 构造
├─ mailqueue.py     持久化发信队列、幂等、退避重试
├─ settings_store.py 非敏感配置覆盖
├─ token_store.py   远程接入令牌(只存 sha256,明文只在创建时返回一次)
├─ scheduler.py     后台任务(ICS 修复、队列轮询、快照清理)
├─ backup.py        在线备份、恢复、清理
├─ bootstrap.py     迁移与启动检查
├─ cli.py           运维命令
└─ routers/         health / feed / auth / pages / api_*
```

## 必须保持的不变量

改动前请确认不会破坏以下约束,它们都有对应测试:

1. **时间一律以 naive UTC 落库**;输入带偏移量按偏移解释,不带偏移量按事件的 IANA 时区解释。
   展示与 ICS 生成时再还原。见 `test_naive_input_uses_event_timezone`。
2. **UID 创建后永不变更**;修改才递增 `sequence`,无实际变化不递增、不产生新版本。
   见 `test_uid_is_stable_across_exports`、`test_noop_update_does_not_create_new_version`。
3. **导出内容必须只取决于数据库状态,不能取决于导出时刻。**
   导出器用内容哈希判断是否需要新版本,所以 `DTSTAMP` 等字段一律取事件自身的修订时间
   (`updated_at`),绝不能取挂钟时间。否则跨秒重新导出会得到新哈希,导致没有改动的保存
   也递增版本、并向所有同步人员重发完整日历。见 `test_content_is_deterministic_across_wall_clock`。
4. **发布文件只做原子替换**:先写同目录临时文件并 `fsync`,再 `os.replace`。
   订阅端只能读到完整的旧文件或新文件。见 `test_atomic_replace_never_exposes_partial_file`。
5. **导出失败不得丢数据**:事件与 `dirty` 先提交,再生成 ICS;失败时回滚导出事务并
   单独补记 `last_error`,订阅端继续返回最近的有效文件。见 `test_feed_serves_last_valid_file_when_export_fails`。
6. **邮件任务幂等键是 `(export_version, recipient_id)`**,唯一约束在数据库层;
   附件永远取自该任务记录的不可变快照,重试不得改用更新版本。见 `tests/test_mail.py`。
7. **SMTP 口令只来自环境变量**,不写数据库、不写日志、不回显;日志中的邮箱一律脱敏
   (`app/util.mask_email`)。见 `test_smtp_password_is_never_persisted`。
8. **管理接口必须登录 + CSRF**;`/calendar` 与 `/calendar.ics` 只有 GET/HEAD 且公开。
9. **全天事件的 `end_date_exclusive` 不含在事件内**;ICS 里 `DTEND` 是同义的不含边界日期。
10. **默认时区相关查询**:`GET /api/v1/events` 只有显式给了 `start`/`end` 才做日期过滤,
    否则关键词检索应覆盖全部事件。
11. **远程 Agent 接口(`/api/agent`)**只认 `Authorization: Bearer`,不接受 Cookie 会话
    (因此没有 CSRF 面,也意味着已登录会话不能调用它);令牌有两个来源并**取并集**——
    `.env` 的 `AGENT_API_TOKEN` 与数据库 `agent_tokens` 表;两个来源都没有令牌时整段返回 404
    而不是 401,避免暴露接口存在;`client_ref` 命中时必须原样返回既有事件,不得新建、
    不得重新导出、不得发信。见 `tests/test_api_agent.py`。
12. **远程接入令牌只存 `sha256` 哈希**:明文仅允许出现在**创建成功那一次响应**里,
    之后不得回显;库、日志、审计 detail 中都不允许出现明文,页面只展示指纹
    (哈希前 16 位,与审计日志 `agent:<指纹>` 一致)。见 `tests/test_agent_tokens.py`。

## 部署时的两个陷阱

- **Tailscale Serve 会剥掉挂载前缀**:`--set-path=/calendar` 会把 `/calendar/admin` 以 `/admin`
  转发给后端。目标地址必须写成 `http://127.0.0.1:443/calendar`,否则管理界面会暴露给整个
  tailnet。改完务必确认 `/calendar/login` 返回 404。详见《部署与运维》第 3 节。
- **Docker NAT 让本机请求与代理请求同源**:容器内看到的对端都是 Docker 网关(`172.19.0.1`),
  应用层无法区分「本机浏览器」与「经 Tailscale 来的请求」,因此管理面隔离只能靠挂载路径保证,
  不要在应用里靠客户端地址做判断。

## 约定

- 所有用户可见文案与注释使用简体中文;代码标识符使用英文。
- 数据库结构变更必须新增 Alembic 迁移(`migrations/versions/`),不要修改已应用的迁移。
- `alembic.ini` 必须保持纯 ASCII:它以系统 locale 编码读取,中文会导致 `UnicodeDecodeError`。
- 修改行为时同步更新测试与 `docs/验收测试记录.md` 中的对应条目。
- 新增敏感字段时先问:它会不会进入日志、数据库或镜像?参照第 6 条处理。
- 前端不使用内联脚本:管理页启用了严格 CSP(`script-src 'self'`),JS 放在 `static/js/`,
  需要传给前端的值通过 `data-*` 属性传递。
