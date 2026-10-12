# `app/routers/api_settings.py`

应用设置 API(非敏感配置)。本次变更:读接口新增 `timezone_choices`;
家庭版把读写依赖从「登录 + CSRF」收紧为**管理员专属**。

## 变更的函数

### `_serialize(session) -> dict[str, object]`

- **位置**:`app/routers/api_settings.py:30`
- **本次变更**:返回体新增
  ```json
  "timezone_choices": [{"index": 1, "iana": "Asia/Shanghai", "label": "北京时间", "region": "亚洲"}, ...]
  ```
- **用途**:让快捷指令 / 脚本能自取序号表,不必硬编码或去翻文档
- **不变量**:不含任何凭据;`smtp.password_configured` 仍只暴露布尔值
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

- **位置**:`app/routers/api_settings.py:63`
- **本次变更**(2026-10-08):依赖由 `CsrfHeaderDep` 改为 `CsrfHeaderAdminDep`
  —— 现在需要**登录 + `X-CSRF-Token` 请求头 + 管理员**三者同时满足;成员 403
- **本次变更**:无逻辑改动;`default_timezone` 现在可以提交序号(如 `1`),
  解析与拒绝都在 `app/settings_store.py::validate_overrides`
- **用途**:`PATCH /api/v1/settings` 局部更新非敏感配置
- **参数**:`payload` 只取显式提交且非 `None` 的字段(`exclude_unset=True, exclude_none=True`);
  空提交直接回读快照,不报错
- **副作用**:写 `settings` 覆盖表并提交;校验失败回滚并返回 422 字段错误
- **测试**:`test_settings_api_accepts_timezone_index`、
  `tests/test_api_settings.py::test_settings_update_and_validation`

## 同类改动:`app/routers/api_sync.py`(管理员专属化)

本次把同步/邮件相关的写接口从 `CsrfHeaderDep` 统一改成 `CsrfHeaderAdminDep`;
读接口原本就是 `AdminDep`。**没有别的逻辑改动**。

| 接口 | 函数 | 位置 | 依赖 |
|---|---|---|---|
| `GET /api/v1/sync/status` | `sync_status` | `app/routers/api_sync.py:64` | `AdminDep`(不变) |
| `POST /api/v1/sync/calendar` | `regenerate_calendar` | `app/routers/api_sync.py:70` | `CsrfHeaderAdminDep`(收紧) |
| `POST /api/v1/sync/mail/send-latest` | `send_latest` | `app/routers/api_sync.py:93` | `CsrfHeaderAdminDep`(收紧) |
| `GET /api/v1/mail-deliveries` | `list_deliveries` | `app/routers/api_sync.py:142` | `AdminDep`(不变) |
| `POST /api/v1/mail-deliveries/{id}/retry` | `retry_one` | `app/routers/api_sync.py:151` | `CsrfHeaderAdminDep`(收紧) |
| `POST /api/v1/settings/mail/test` | `mail_test` | `app/routers/api_sync.py:188` | `CsrfHeaderAdminDep`(收紧) |

- **不变量**:管理接口必须**登录 + CSRF + 管理员**;成员一律 403,
  即使他是某条共享日历的 `editor`
- **不变量**:日志与响应里的邮箱一律脱敏(`app/util.mask_email`);SMTP 口令不回显
- **测试**:`tests/test_family_pages.py::test_member_cannot_use_admin_apis`(逐个断言 403)、
  `tests/test_api_settings.py::test_settings_update_and_validation`、
  `::test_regenerate_creates_new_version`、`::test_send_latest_requires_confirmation`

## 相关接口

- `GET /api/v1/settings` 需要管理员会话;`PATCH` 还需要 CSRF 头。
- 事件接口的 `timezone` 序号解析在 `app/events.py`,与本接口共用 `app/timezones.py` 的清单。
- 收件人接口(`app/routers/api_recipients.py`)同样管理员专属,见
  [`app-routers-api-recipients.md`](app-routers-api-recipients.md)。

## 变更记录

- 2026-09-29:读接口新增 `timezone_choices`;`default_timezone` 支持序号。
- 2026-10-08:家庭版把 `read_settings` 收紧为 `AdminDep`、`update_settings` 收紧为
  `CsrfHeaderAdminDep`;并顺带归档 `app/routers/api_sync.py` 的同类改动
  (三个写接口改用 `CsrfHeaderAdminDep`)。