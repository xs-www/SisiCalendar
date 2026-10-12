# `app/schemas.py`

Pydantic 输入/输出模型。本次变更:新增 `EventWrite.url` / `EventWrite.reminders` 与
`EventRead.reminders` / `EventRead.reminder_labels`;家庭版新增 `EventWrite.calendar_id`
与 `EventRead` 的日历/权限字段;本轮(2026-10-12)把 `SyncStatusRead.subscription_url`
改名为 `public_base_url`。

## 函数

### `_reminders_before(value) -> Any`

- **位置**:`app/schemas.py:31`,挂在 `EventWrite.reminders`(`app/schemas.py:97`,
  校验器 `app/schemas.py:128`)
- **用途**:让表单形态(如 iOS 快捷指令的 `application/x-www-form-urlencoded`)直接提交
  `reminders=15,1440`,等价于 JSON 的 `[15, 1440]`
- **参数**:任意原始输入;非字符串原样返回(JSON 数组交给 pydantic 做成员类型校验)
- **返回**:解析出的 `list[int]`;空字符串按「未提供」返回 `[]`
- **校验**:任一片段不是整数分钟数时抛 `ValueError`,文案为 `REMINDER_FORMAT_HINT`(中文),
  由路由的 `field_errors_from` 转成 422 字段错误——**拼错的参数必须失败,不能被猜中**
- **不变式**:范围/去重/排序仍由 `app/events.py::normalize_event_input` 统一负责,
  本函数只做形态归一
- **测试**:`tests/test_reminders.py::test_api_accepts_reminders_as_comma_string`、
  `::test_api_rejects_invalid_reminders`、`::test_agent_api_rejects_typo_in_form_reminders`

### `_timezone_before(value) -> Any`

- **位置**:`app/schemas.py:57`
- **用途**:`timezone` / `default_timezone` 的 `mode="before"` 校验器:把 JSON 里的数字
  (`1`)转成字符串 `"1"`,使 `"timezone": 1` 与 `"timezone": "1"` 行为一致
- **参数**:任何原始输入;`bool` 与 `None` 原样返回(避免 `True` 被当成序号 1)
- **返回**:字符串序号 / 原值
- **说明**:真正的语义解析在 `app.timezones.resolve_timezone`,这里只做类型归一
- **测试**:`tests/test_timezones.py::test_events_api_accepts_timezone_index`

## 模型字段

### `EventWrite.timezone`

- **位置**:`app/schemas.py:92`(校验器挂在 `app/schemas.py:126`)
- **类型**:`str | None`,默认 `None`;经 `_normalize_timezone` 预处理
- **接受**:IANA 名、中文名、序号(数字或数字字符串)
- **不变式**:模型层不校验时区合法性——非法值留给
  `app/events.py::normalize_event_input` 统一返回 422 字段错误

### `SettingsUpdate.default_timezone`

- **位置**:`app/schemas.py:199`(校验器挂在 `app/schemas.py:205`)
- **类型**:`str | None`,同样经 `_normalize_timezone`
- **校验**:`app/settings_store.py::validate_overrides` 负责解析与拒绝

### `EventWrite.url` / `EventRead.url`

- **位置**:`app/schemas.py:95` / `app/schemas.py:137`
- **类型**:`str | None`,最长 1000 字符;备注里的链接,导出为 ICS 的 `URL` 属性
- **说明**:不做 URL 语法校验——`http(s)://` 之外的值(如 `x-apple-reminder://`)也应能存

### `EventWrite.reminders` / `EventRead.reminders` / `EventRead.reminder_labels`

- **位置**:`app/schemas.py:97` / `app/schemas.py:153`
- **类型**:`list[int] | None`(写入)、`list[int]`(读取,默认空列表)+ 中文描述列表
- **写入约束**:`max_length=MAX_REMINDER_COUNT`(50),超出返回 422 而不是被截断
- **读取形态**:已由 `app/reminders.py` 去重并**从大到小**排序,`reminder_labels` 与之一一对应
- **测试**:`tests/test_reminders.py::test_api_rejects_invalid_reminders`

## 变更(2026-10-08:事件归属日历)

### `EventWrite.calendar_id`

- **位置**:`app/schemas.py:120`(字段上限 `max_length=36`)
- **类型**:`str | None`,默认 `None`
- **语义**:新建时决定事件落到哪条日历(**必须可写**);PATCH 时相当于「移动到日历」;
  不给时新建用默认可写日历、修改沿用原日历
- **模型层只做长度校验**:可见/可写判定在路由
  (`app/routers/api_events.py::_resolve_write_calendar`),越界读 404、不可写 403
- **不变量**:不得因为请求体里带了一个 id 就跳过 `CalendarAccess` 判定
- **测试**:`tests/test_family_web.py::test_member_cannot_write_to_shared_calendar`、
  `::test_admin_can_move_event_between_calendars`、
  `::test_admin_cannot_move_event_into_member_private_calendar`

### `EventRead` 新增字段

- **位置**:`app/schemas.py:144`-`149`
- **字段**:
  - `calendar_id: str | None = None` —— 事件所属日历;历史数据在引导时回填,允许为空
  - `calendar_name` / `calendar_color` / `calendar_kind: str | None = None` —— 只有在
    **带 `access` 序列化**时才填充(`app/events.py::serialize_event`,`app/events.py:749`),
    界面据此显示日历名与颜色
  - `can_write: bool = False` —— 当前调用者能否改这条事件
  - `created_by_user_id: str | None = None` —— 创建人账号;删号后由外键置空
- **不变量**:`can_write` 必须来自 `CalendarAccess.can_write(event.calendar_id)`,
  不得由事件自身字段或来源推导
- **测试**:`tests/test_family_web.py::test_calendars_endpoint_lists_visible_calendars_with_roles`、
  `::test_member_can_create_in_own_personal_calendar`、
  `tests/test_family_mail.py::test_agent_token_writes_into_its_bound_calendar`

## 变更(2026-10-12:同步状态里的公开基地址)

### `SyncStatusRead.public_base_url`

- **位置**:`app/schemas.py:243`(模型类 `SyncStatusRead` 在 `app/schemas.py:235`)
- **本次变更**:原来叫 `subscription_url` 的那个必填字段**改名为 `public_base_url`**
  (`git diff app/schemas.py` 可见 `-    subscription_url: str` → `+    public_base_url: str`)。
  仓库里**没有**名为 `SyncState` 的 Pydantic 模型,承载该字段的就是 `SyncStatusRead`
- **类型**:`str`,**必填、无默认值**
- **语义**:对外可复制的**公开基地址**(`PUBLIC_BASE_URL`,含路径前缀,如
  `https://宿主/calendar`),**不是**某个令牌的订阅地址、也不是旧的无令牌地址 ——
  旧 `SUBSCRIPTION_URL` 与 `/calendar`、`/calendar.ics` 都不再存在
- **谁填它**:`app/sync_service.py::state_snapshot()`(`app/sync_service.py:220`)写入
  `urls.public_base_url(resolved)`;`app/routers/api_settings.py::_serialize()`
  (`app/routers/api_settings.py:39`)用同名键返回同一个值;`app/overview()` 在它的基础上
  追加队列与收件人统计,所以 `/api/v1/sync/status` 与页面 `sync_state` 都带这个键
- **不变量**:字段名与模板引用必须同步 —— 目前 `templates/partials/sync_status.html:23`
  仍在读 `sync_state.subscription_url`,因而渲染出空值(见 `docs/static/templates.md`)。
  改动这个字段时要一并核对模板与 `tests/test_timezones.py` 里手工构造的 `sync_state` 字典
- **测试**:`tests/test_api_settings.py` 的设置/同步接口用例间接覆盖;
  **没有**专门断言 `public_base_url` 存在且非空的用例(见 `docs/static/app-routers-api-settings.md`)

## 变更记录

- 2026-09-29:新增 `_timezone_before`,挂到 `EventWrite.timezone` 与 `SettingsUpdate.default_timezone`。
- 2026-09-30:新增 `_reminders_before` 与 `EventWrite.url` / `EventWrite.reminders`;
  `EventRead` 增加 `url` / `reminders` / `reminder_labels`。
- 2026-10-08:家庭版新增 `EventWrite.calendar_id` 与 `EventRead.calendar_id` /
  `calendar_name` / `calendar_color` / `calendar_kind` / `can_write` / `created_by_user_id`;
  同时按当前源码校正了全部既有条目的行号。
- 2026-10-12:`SyncStatusRead` 的 `subscription_url` 改名为 `public_base_url`
  (同步状态里给的是公开基地址,不是某个订阅地址),并写明谁填它、模板引用尚未跟上。
