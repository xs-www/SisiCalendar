# `app/routers/api_settings.py`

应用设置 API(非敏感配置)。本次变更:读接口新增 `timezone_choices`;
家庭版把读写依赖从「登录 + CSRF」收紧为**管理员专属**;
2026-10-14 起 `PATCH` 可以提交 `public_base_url`(**只收 origin**)。

## 变更的函数

### `_serialize(session) -> dict[str, object]`

- **位置**:`app/routers/api_settings.py:31`
- **本次变更**:返回体新增
  ```json
  "timezone_choices": [{"index": 1, "iana": "Asia/Shanghai", "label": "北京时间", "region": "亚洲"}, ...]
  ```
- **用途**:让快捷指令 / 脚本能自取序号表,不必硬编码或去翻文档
- **返回**:
  - `public_base_url`(`app/routers/api_settings.py:39`)= `urls.public_base_url(settings)`,
    其中 `settings = effective_settings(session)` —— 所以它是**拼好挂载前缀的完整地址**,
    库里的 origin 覆盖会体现在这里;
  - `overrides` = `load_overrides(session)`,里面的 `public_base_url` 是**库里的 origin**
    (只有 origin,没有路径)
- **不变量**:不含任何凭据;`smtp.password_configured` 仍只暴露布尔值;
  两个 `public_base_url` 的语义不同,别混用(顶层是完整地址,`overrides` 里是 origin)
- **测试**:`tests/test_timezones.py::test_settings_api_exposes_timezone_choices`

### `read_settings(admin: AdminDep, session) -> dict[str, object]`

- **位置**:`app/routers/api_settings.py:58`
- **本次变更**(2026-10-08):依赖 `AdminDep`(登录 + 管理员);
  成员调用返回 **403**,不再是「任何登录用户都能读」
- **用途**:`GET /api/v1/settings` 返回非敏感配置全量快照
- **返回**:`_serialize(session)` 的字典
- **不变量**:SMTP 口令永远只以 `password_configured` 布尔值出现
- **测试**:`tests/test_family_pages.py::test_member_cannot_use_admin_apis`、
  `tests/test_api_settings.py::test_settings_never_exposes_smtp_password`

### `update_settings(payload, admin: CsrfHeaderAdminDep, session) -> dict[str, object]`

- **位置**:`app/routers/api_settings.py:64`
- **本次变更**(2026-10-08):依赖由 `CsrfHeaderDep` 改为 `CsrfHeaderAdminDep`
  —— 现在需要**登录 + `X-CSRF-Token` 请求头 + 管理员**三者同时满足;成员 403
- **本次变更**:无逻辑改动;`default_timezone` 现在可以提交序号(如 `1`),
  解析与拒绝都在 `app/settings_store.py::validate_overrides`
- **本次变更(2026-10-14)**:可以提交 `public_base_url`(origin);见下一节
- **用途**:`PATCH /api/v1/settings` 局部更新非敏感配置
- **参数**:`payload` 只取显式提交且非 `None` 的字段(`exclude_unset=True, exclude_none=True`);
  空提交直接回读快照,不报错
- **副作用**:写 `settings` 覆盖表并提交;校验失败回滚并返回 422 字段错误
- **测试**:`test_settings_api_accepts_timezone_index`、
  `tests/test_api_settings.py::test_settings_update_and_validation`

## 2026-10-14:设置接口接受「对外地址」的 origin

### `PATCH /api/v1/settings` — `public_base_url`

- **位置**:字段定义 `app/schemas.py:205`;处理在 `app/routers/api_settings.py:69`–`:74`
- **请求**:`{"public_base_url": "https://other-node.ts.net"}` —— 只收 **origin**;
  允许 `主机` / `主机:端口` / 完整 URL,**写了路径就必须与挂载前缀逐字一致**
- **响应**:**两个同名键语义不同**
  - `public_base_url` = **拼好的完整地址**(如 `https://other-node.ts.net/calendar`);
  - `overrides.public_base_url` = **库里的 origin**(`https://other-node.ts.net`)
- **非法路径**:422,错误体为
  `{"error": {"code": "validation_error", "message": "设置校验失败",
  "fields": {"public_base_url": "…路径前缀…环境变量…"}}}` ——
  由 `SettingValidationError` 经 `error_payload("validation_error", …)` 转成字段错误,
  文案说明「路径前缀只能改环境变量 `PUBLIC_BASE_URL` 并重建容器」
- **空值**:`""` = 撤销覆盖、回到环境变量(`save_overrides` 删除该行)
- **副作用**:写库(只写 origin)+ 提交;审计不涉及(接口层只记一条 `logger.info`)
- **不变量**:
  - API 与页面**共用同一套校验**(`app/settings_store.py::validate_overrides`),
    不得在路由里另写一份 URL 规则;
  - `update_settings` 调的是 `save_overrides(session, values)` —— **不传** `settings`,
    于是路径规则取 `get_settings()` 的挂载前缀;页面那条路径传的是
    `effective_settings(session)`。两者的前缀都只来自环境变量,行为一致;
  - 不接受路径前缀的写入 —— 路由是启动时按 `PUBLIC_BASE_URL` 的路径挂载的
- **测试**:`tests/test_settings_public_base_url.py::test_api_accepts_the_origin_and_returns_the_composed_url`
  (响应断言见上)、`::test_api_rejects_a_different_path`(422,响应正文含 `public_base_url`)

## 同类改动:`app/routers/api_sync.py`(管理员专属化)

本次把同步/邮件相关的写接口从 `CsrfHeaderDep` 统一改成 `CsrfHeaderAdminDep`;
读接口原本就是 `AdminDep`。**没有别的逻辑改动**。

| 接口 | 函数 | 位置 | 依赖 |
|---|---|---|---|
| `GET /api/v1/sync/status` | `sync_status` | `app/routers/api_sync.py:64`(函数 `:65`) | `AdminDep`(不变) |
| `POST /api/v1/sync/calendar` | `regenerate_calendar` | `app/routers/api_sync.py:70`(函数 `:71`) | `CsrfHeaderAdminDep`(收紧) |
| `POST /api/v1/sync/mail/send-latest` | `send_latest` | `app/routers/api_sync.py:99`(函数 `:100`) | `CsrfHeaderAdminDep`(收紧) |
| `GET /api/v1/mail-deliveries` | `list_deliveries` | `app/routers/api_sync.py:156`(函数 `:157`) | `AdminDep`(不变) |
| `POST /api/v1/mail-deliveries/{id}/retry` | `retry_one` | `app/routers/api_sync.py:165`(函数 `:166`) | `CsrfHeaderAdminDep`(收紧) |
| `POST /api/v1/settings/mail/test` | `mail_test` | `app/routers/api_sync.py:202`(函数 `:203`) | `CsrfHeaderAdminDep`(收紧) |

- **不变量**:管理接口必须**登录 + CSRF + 管理员**;成员一律 403,
  即使他是某条共享日历的 `editor`
- **不变量**:日志与响应里的邮箱一律脱敏(`app/util.mask_email`);SMTP 口令不回显
- **测试**:`tests/test_family_pages.py::test_member_cannot_use_admin_apis`(逐个断言 403)、
  `tests/test_api_settings.py::test_settings_update_and_validation`、
  `::test_regenerate_creates_new_version`、`::test_send_latest_requires_confirmation`

## 2026-10-12(第二轮):发信任务多返回「发起人」

| 接口 | 变化 |
|---|---|
| `GET /api/v1/mail-deliveries` | `_recent_deliveries()` 每项多一个 `actor_label`(发起人显示名快照);历史任务为 `null` |
| `POST /api/v1/sync/mail/send-latest` | `publish(...)` / `send_latest_to_enabled(...)` 改为显式传入 `admin.user_id` / `admin.label` |
| `POST /api/v1/sync/calendar` | 同上(重新生成也记录操作者,但正文不写这一行) |

- **位置**:`app/routers/api_sync.py:38`(`_recent_deliveries`,`actor_label` 在 `:51`)、
  `:89`–`:90`、`:132`、`:141`–`:142`
- **返回**:`items[].actor_label`(`str | None`),契约在
  `app/schemas.py::DeliveryRead`(新增字段带默认值 `None`,老客户端不受影响)
- **不变量**:`actor_label` 是**快照**;接口不返回 `actor_user_id`,
  也不做任何额外查询 —— 队列要还原「当时是谁」,发信正文才用现查的显示名
- **测试**:`tests/test_mail_actor.py::test_queue_page_and_api_expose_the_actor`

## 相关接口

- `GET /api/v1/settings` 需要管理员会话;`PATCH` 还需要 CSRF 头。
- 事件接口的 `timezone` 序号解析在 `app/events.py`,与本接口共用 `app/timezones.py` 的清单。
- 收件人接口(`app/routers/api_recipients.py`)同样管理员专属,见
  [`app-routers-api-recipients.md`](app-routers-api-recipients.md)。
- 「对外地址」的界面入口在设置页卡片(`app/routers/pages.py::_settings_form_context`),
  宿主端口探测见 [`app-host-port.md`](app-host-port.md)。

## 变更记录

- 2026-09-29:读接口新增 `timezone_choices`;`default_timezone` 支持序号。
- 2026-10-08:家庭版把 `read_settings` 收紧为 `AdminDep`、`update_settings` 收紧为
  `CsrfHeaderAdminDep`;并顺带归档 `app/routers/api_sync.py` 的同类改动
  (三个写接口改用 `CsrfHeaderAdminDep`)。
- 2026-10-12(第二轮):`/api/v1/mail-deliveries` 每项新增 `actor_label`(发起人快照);
  两个手动动作显式传当前管理员为发起人。测试见 `tests/test_mail_actor.py`。
- 2026-10-14:`PATCH /api/v1/settings` 接受 `public_base_url`(只收 origin):
  请求给 origin、顶层响应给**拼好的完整地址**、`overrides.public_base_url` 给库里的
  origin,非法路径 422(字段错误);`_serialize` 的位置校正为 `:31`、`update_settings` 为 `:64`。
  测试:`tests/test_settings_public_base_url.py::test_api_accepts_the_origin_and_returns_the_composed_url`、
  `::test_api_rejects_a_different_path`。同轮把 `api_sync.py` 表格里漂移的行号一并校正
  (`sync_status` `:64`、`regenerate_calendar` `:70`、`send_latest` `:99`、
  `list_deliveries` `:156`、`retry_one` `:165`、`mail_test` `:202`)。