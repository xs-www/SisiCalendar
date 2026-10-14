# 静态函数文档

本目录按「一个源码文件一份文档」归档各模块的**静态函数契约**:签名、参数、返回值、
副作用与对应测试。它是给后续 Agent 的索引——改代码前先看这里,不必一上来通读全部实现。

## 格式约定

每个函数一条,固定字段:

```markdown
### `name(args) -> ret`

- **位置**:`app/module.py:行号`
- **用途**:一句话说明对外行为,不写实现细节
- **参数**:只写非显然的约束(取值范围、单位、时区语义)
- **返回**:返回值语义与特殊值(`None` 代表什么)
- **副作用**:写库、发信、文件替换、限流等;无则写「无」
- **不变量**:改动时必须保持的约束(如「序号不得重排」)
- **测试**:`tests/test_x.py::test_y`
- **变更记录**:日期 + 变更摘要
```

## 索引

### 数据、配置与安全

| 文档 | 源码 |
|---|---|
| [`app-models.md`](app-models.md) | `app/models.py`(账号/日历/成员/邀请码/订阅令牌,事件归属列与收件人身份/范围列) |
| [`migrations-0005.md`](migrations-0005.md) | `migrations/versions/0005_family_accounts.py`(家庭版结构变更) |
| [`migrations-0006.md`](migrations-0006.md) | `migrations/versions/0006_recipient_scope.py`(收件人身份与范围拆分,修复重复绑定) |
| [`migrations-0007.md`](migrations-0007.md) | `migrations/versions/0007_email_verification.py`(`users` 加 5 个邮箱验证状态列,老邮箱回填为已验证) |
| [`migrations-0008.md`](migrations-0008.md) | `migrations/versions/0008_delivery_actor.py`(`mail_deliveries` 加 `actor_user_id`/`actor_label`,只加列不回填) |
| [`app-bootstrap.md`](app-bootstrap.md) | `app/bootstrap.py`(家庭版数据引导 `ensure_family_setup`) |
| [`app-config.md`](app-config.md) | `app/config.py`(派生路径 `feed_dir`、公开基地址与邮箱验证阈值) |
| [`app-security.md`](app-security.md) | `app/security.py`(会话盐值 v2 与 `create_session`/`read_session`) |
| [`app-email-verify.md`](app-email-verify.md) | `app/email_verify.py`(验证码状态机、闸门豁免规则与两封通知信) |
| [`app-settings-store.md`](app-settings-store.md) | `app/settings_store.py` |
| [`app-schemas.md`](app-schemas.md) | `app/schemas.py`(含 `EventWrite.calendar_id` 与 `EventRead` 日历/权限字段) |

### 账号、日历与令牌

| 文档 | 源码 |
|---|---|
| [`app-accounts.md`](app-accounts.md) | `app/accounts.py`(账号、私人/共享日历、成员角色、`CalendarAccess`、收件人身份/范围) |
| [`app-invites.md`](app-invites.md) | `app/invites.py`(邀请码,只存哈希,次数与有效期) |
| [`app-feed-tokens.md`](app-feed-tokens.md) | `app/feed_tokens.py`(ICS 订阅令牌,只存哈希) |
| [`app-token-store.md`](app-token-store.md) | `app/token_store.py`(远程接入令牌,含账号/日历作用域) |
| [`app-deps.md`](app-deps.md) | `app/deps.py`(当前用户与 CSRF/管理员依赖、Agent 令牌作用域、`localtime`) |

### 事件与导出

| 文档 | 源码 |
|---|---|
| [`app-events.md`](app-events.md) | `app/events.py`(多日历归属与 `calendar_ids` 范围过滤) |
| [`app-timeutil.md`](app-timeutil.md) | `app/timeutil.py`(提交偏移量 `input_utc_offset`) |
| [`app-timezones.md`](app-timezones.md) | `app/timezones.py`(常用时区清单与序号解析) |
| [`app-reminders.md`](app-reminders.md) | `app/reminders.py`(提醒取值、预设与中文描述) |
| [`app-ics.md`](app-ics.md) | `app/ics.py`(`VALARM` 提醒与 `URL` 导出) |
| [`app-exporter.md`](app-exporter.md) | `app/exporter.py`(多目标导出、目标键与按目标快照) |

### 同步与发信

| 文档 | 源码 |
|---|---|
| [`app-sync-service.md`](app-sync-service.md) | `app/sync_service.py`(`publish` 的多目标编排与发起人参数) |
| [`app-mailqueue.md`](app-mailqueue.md) | `app/mailqueue.py`(按收件人目标取附件、正文范围提示与「发起人」一行) |

### 交付链路

| 文档 | 源码 |
|---|---|
| [`ci-docker-publish.md`](ci-docker-publish.md) | `.github/workflows/docker-publish.yml` + `compose.nas.yaml`(门禁、标签规则、双架构与拉取部署契约) |

### 路由、装配与模板

| 文档 | 源码 |
|---|---|
| [`app-main.md`](app-main.md) | `app/main.py`(403 处理器与 CSP 路径集合) |
| [`app-urls.md`](app-urls.md) | `app/urls.py`(公开面前缀绑定、站内链接与邀请/订阅绝对地址) |
| [`app-routers-pages.md`](app-routers-pages.md) | `app/routers/pages.py`(成员/管理员页面与全部片段) |
| [`app-routers-auth.md`](app-routers-auth.md) | `app/routers/auth.py`(登录/登出/邀请码注册) |
| [`app-routers-feed.md`](app-routers-feed.md) | `app/routers/feed.py`(按令牌范围的只读订阅) |
| [`app-routers-api-events.md`](app-routers-api-events.md) | `app/routers/api_events.py`(日历列表与事件 CRUD 的可见/可写判定) |
| [`app-routers-api-agent.md`](app-routers-api-agent.md) | `app/routers/api_agent.py`(Agent 作用域与 `client_ref` 幂等边界) |
| [`app-routers-api-recipients.md`](app-routers-api-recipients.md) | `app/routers/api_recipients.py`(账号/历史绑定/家庭三类收信范围与确认参数) |
| [`app-routers-api-settings.md`](app-routers-api-settings.md) | `app/routers/api_settings.py` + `api_sync.py`(管理员专属化) |
| [`templates.md`](templates.md) | `templates/`(家庭版清单、角色分级导航、无内联脚本) |

## 本轮变更(2026-10-14)

1. **新增交付链路(不改 `app/` 代码、不加迁移)**:
   `.github/workflows/docker-publish.yml` 在 `master` 推送后先跑 `ruff` + `pytest` 门禁,
   再构建 `linux/amd64` + `linux/arm64` 双架构镜像并推送到 `ghcr.io/xs-www/sisicalendar`;
   新增 `compose.nas.yaml` 供 NAS 纯拉取部署(`image:` 替代 `build:`,服务名仍为 `calendar`,
   端口只绑 `127.0.0.1:18443`)。契约见 [`ci-docker-publish.md`](ci-docker-publish.md);
   操作步骤见《部署与运维》§2.9 / §5.1 / §5.2 与 `README.md` 的「部署」节。

## 本轮变更(2026-10-12)

1. **公开面统一到 `/calendar` 前缀**。配置项 `PUBLIC_BASE_URL`(默认
   `https://sisi-laptop.tail921448.ts.net/calendar`)取代并**删除了**原来的 `SUBSCRIPTION_URL`;
   它的 **URL 路径部分**(`/calendar`)就是公开面的挂载前缀。公开面因此是
   `/calendar/register`、`/calendar/login`、`/calendar/admin/...`、`/calendar/verify-email`、
   `/calendar/api/...`、`/calendar/static/...`、`/calendar/<令牌>.ics`。
   本机管理面(`http://localhost:443/admin` 等)**裸路径仍然可用**:裸路由与带前缀路由
   都注册了,带前缀的那一套 `include_in_schema=False`。
2. **新增 `app/urls.py`**:`_PREFIX: ContextVar[str]`、`normalize_prefix`、`public_base_url`、
   `public_base_path`、`bind_prefix`、`current_prefix`、`url`、`relative`、`is_api_path`、
   `is_admin_path`、`invite_url`(→ `{base}/register?code=…`,可直接复制粘贴)、
   `subscription_url`(→ `{base}/{token}.ics`)。前缀由 `app/main.py` 的 HTTP 中间件在
   路由前调用 `bind_prefix` 绑定;Jinja 里通过全局 `bp()` / `url()` 使用。
   见 [`app-urls.md`](app-urls.md)。
3. **旧的无令牌订阅地址彻底取消**:`/calendar` 与 `/calendar.ics` 不再提供日历。
   前缀**根**本身(`/calendar`)对日历客户端(`Accept` 非 `text/html`)返回 404 纯文本
   (文案在 `app/routers/pages.py::FEED_PLACEHOLDER_TEXT`),对浏览器(`Accept` 含 `text/html`)
   303 到 `/calendar/admin`。原来「旧地址服务首位管理员可见范围」的行为已删除。
4. **每个账号一条固定订阅地址**:
   `token = sisi_feed_ + HMAC(session_secret, "feed:<user_id>")[:40]`,由服务端派生、
   **永不变更**;账号创建时(以及引导回填)登记一行 `feed_tokens`
   (标签「账号订阅地址(固定)」,库里只存 `sha256`),界面**不给停用/撤销**(409)。
   轮换 `data/session_secret` 会让所有固定地址与验证码哈希失效(刻意的取舍)。
   见 [`app-feed-tokens.md`](app-feed-tokens.md)。
5. **新增 `app/email_verify.py`(邮箱验证)**:6 位验证码,30 分钟有效,最多错 5 次
   (第 5 次作废),重发冷却 60 秒;验证码哈希 =
   `HMAC(session_secret, f"verify:{user_id}:{code}")`,库里只存哈希;
   非管理员「有邮箱但未验证」被闸门挡住(页面 303 → `/verify-email`,
   `/api` 返回 403 且 `error.code == "email_not_verified"`),也不能成为邮件收件人
   (`app/accounts.py::sync_user_recipient` 要求有邮箱**且**已验证);首位管理员豁免;
   验证通过后补发一封「你的固定订阅地址」邮件;改邮箱立刻打回待验证并重发验证码。
   见 [`app-email-verify.md`](app-email-verify.md)。
6. **新增迁移 `0007_email_verification`**(`down_revision = 0006_recipient_scope`):
   batch 给 `users` 加 `email_verified_at`、`email_verify_code_hash`、
   `email_verify_expires_at`、`email_verify_attempts`、`email_verify_sent_at` 5 列,
   并给**已有非空邮箱**的账号回填 `email_verified_at = created_at`
   (否则老成员会被新闸门挡住);`downgrade()` 删列。见 [`migrations-0007.md`](migrations-0007.md)。
7. **同步邮件标明操作发起人**(2026-10-12 第三轮):
   `app/sync_service.py::publish` 新增 `actor_user_id` / `actor_label`,五个路由调用点传入
   当前操作者(**不读** `event.created_by_user_id`,那是创建人不是操作人);
   `app/mailqueue.py::_body_for` 只对新增/修改/删除日程写 `发起人:<显示名>`(手动同步不写),
   名字由 `_actor_name` 现查账号显示名、查不到退回快照、都没有写「未记录」;
   迁移 `0008_delivery_actor` 给 `mail_deliveries` 加两列(**只加列、不回填**),
   删账号时由 `app/accounts.py::delete_user` 把 `actor_user_id` 显式置空、保留 `actor_label`。
   见 [`app-mailqueue.md`](app-mailqueue.md)、[`app-sync-service.md`](app-sync-service.md)、
   [`migrations-0008.md`](migrations-0008.md)。
8. **账号收件人改为派生数据,设置页只维护外部邮箱**(2026-10-12 第四轮):
   新增 `app/accounts.py::sync_account_recipients`,把「启用 + 已验证」的账号对齐成一条**启用**的
   账号收件人(账号停用 → 该行停用但不删),在**启动引导、建发信任务之前、设置页渲染前**
   三个时机调用;`app/sync_service.py::publish` 新增 `changed_calendar_ids`,
   `app/mailqueue.py::ensure_delivery_tasks` 用 `_scope_calendar_ids()` 与它求交集,
   **只给看得见这次改动的收件人建任务**(手动群发/CLI/后台修复不传 = 全员);
   设置页 `/admin/partials/recipients` 的停用/移除与 `PATCH /api/v1/sync-recipients`
   命中账号行一律 409,账号邮箱在页面下方只读列出。
   见 [`app-accounts.md`](app-accounts.md)、[`app-mailqueue.md`](app-mailqueue.md)、
   [`app-sync-service.md`](app-sync-service.md)、[`app-routers-pages.md`](app-routers-pages.md)。

## 与其它文档的分工

- 需求与验收标准:`docs/本地日程与订阅日历服务-开发文档.md`、`docs/验收测试记录.md`
- 部署与排错:`docs/部署与运维.md`
- 行为约束总览:`AGENTS.md`(必须保持的不变量)
- 本目录:函数级契约,回答「这个函数收什么、给什么、改哪些数据」
