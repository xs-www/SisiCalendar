# `app/routers/pages.py`

管理页面与 HTMX 局部片段。本次变更:时区下拉框上下文、邮件队列展示上下文。

## 新增

### `_timezone_context(current=None) -> dict[str, object]`

- **位置**:`app/routers/pages.py:75`
- **用途**:一次性给出时区相关的模板变量,避免各页面各拼一份
- **返回的键**:
  - `timezones`:`all_timezones()` 的全部 IANA 名(新建/编辑页候选)
  - `common_timezones`:`COMMON_TIMEZONES`(带序号的常用清单,缺少 tzdata 时的回退来源)
  - `timezone_choices`:`TIMEZONE_CHOICES`(渲染带序号的候选)
  - `timezone_groups`:`[(地区, [选项...])]`,渲染 `<optgroup>`
  - `timezone_known`:清单内 IANA 名的集合,用于判断是否要补「当前值」选项
  - `current_timezone`:`current` 去空白后的字符串
- **参数**:`current` 为当前生效的时区,用于标记 `<option selected>`
- **副作用**:无
- **测试**:`tests/test_timezones.py::test_settings_page_renders_timezone_dropdown`、
  `::test_settings_page_keeps_custom_timezone`

## 变更

### `COMMON_TIMEZONES`

- **位置**:`app/routers/pages.py:49`
- **本次变更**:由手写 16 项改为 `list(iana_names())`(来自 `app/timezones.py`,30 项)
- **不变式**:`all_timezones()` 在缺少 tzdata 时回退到它,内容必须都是合法 IANA 名

### 页面上下文(默认时区 / 时区候选)

- **位置**:`event_new_page`(`:196`)、`event_edit_page`(`:233`)、`settings_page`(`:264`)、
  `save_settings_action`(`:460`)、`settings_form_partial`(`:732`)
- **本次变更**:原来的 `"timezones": all_timezones()` 改为展开 `**_timezone_context(...)`;
  设置页与设置片段把当前值传成 `overrides.get("default_timezone", settings.default_timezone)`
- **说明**:保存失败时(`save_settings_action` 的 422 分支)下拉框回到**仍然生效**的时区,
  用户填错的原始值只出现在错误提示里

### `_sync_partial(...)`

- **位置**:`app/routers/pages.py:693`
- **本次变更**:上下文新增 `default_timezone = settings.default_timezone`,
  邮件队列表格用它把 `created_at` / `sent_at` / `next_attempt_at` 渲染成本地时间
- **不变式**:`sync_page` 走 `_base_context`,同样提供 `default_timezone`,两条渲染路径行为一致

### `_deliveries(session, limit=30)`

- **位置**:`app/routers/pages.py:97`
- **本次变更**:无(仍按 `created_at` 倒序取最近 30 条);新增列直接使用已有的
  `created_at` / `sent_at`,**不需要数据库迁移**

## `__all__`

- **位置**:`app/routers/pages.py:754`
- **本次变更**:补充导出 `timezone_groups`,方便测试与外部脚本引用清单

## 变更记录

- 2026-09-29:新增 `_timezone_context`;`COMMON_TIMEZONES` 改为清单派生;
  邮件队列上下文补充 `default_timezone`。
