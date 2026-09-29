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

## 新增:远程接入令牌管理片段(HTMX)

路由位于 `app/routers/pages.py:472` 起,渲染 `templates/partials/tokens.html`,
目标元素 `#agent-tokens`,与「同步人员」片段(`#recipients`)同构。

### `_token_partial(request, session, settings, *, notice, error, status_code, new_token) -> Response`

- **位置**:`app/routers/pages.py:475`
- **用途**:统一渲染令牌片段
- **上下文**:`tokens`(`token_store.list_tokens`)、`csrf_token`、`default_timezone`、
  `new_token`(仅创建成功那一次非空)、`notice`、`error`
- **不变量**:`new_token` 只允许出现在**创建成功的响应**里;任何后续渲染都必须为 `None`,
  否则明文会在页面里反复出现

### `GET /admin/partials/tokens`

- **位置**:`app/routers/pages.py:501`;依赖 `AdminDep`(需登录)
- **用途**:列出全部令牌(含已停用)

### `POST /admin/partials/tokens`

- **位置**:`app/routers/pages.py:511`;依赖 `CsrfDep`(需登录 + CSRF)
- **表单**:`label`、`acknowledge`
- **行为**:未勾选 `acknowledge` → 422;`label` 空或超长 → 422;成功 → 200 且响应内含一次性明文
- **副作用**:写库(只写哈希)、审计 `agent_token_add`(detail 为令牌 id,**不含明文**)
- **测试**:`tests/test_agent_tokens.py::test_plaintext_is_shown_once_and_only_hash_is_stored`、
  `::test_creation_requires_acknowledgement_and_label`、`::test_plaintext_never_reaches_logs`

### `POST /admin/partials/tokens/{token_id}` / `.../toggle` / `.../delete`

- **位置**:`app/routers/pages.py:544` / `:572` / `:600`
- **用途**:改名、启用/停用、撤销(硬删除)
- **行为**:id 不存在 → 404;改名校验失败 → 422 且不写入
- **副作用**:写库 + 审计 `agent_token_rename` / `agent_token_toggle` / `agent_token_revoke`
- **说明**:撤销后该令牌**下一次请求即失效**;若因此一枚不剩,`/api/agent` 回到 404
- **测试**:`tests/test_agent_tokens.py::test_token_can_be_renamed`、
  `::test_disabled_token_is_rejected_and_can_be_reenabled`、
  `::test_revoked_token_stops_working_immediately`、`::test_unknown_token_id_returns_404`

### `settings_page` 上下文

- **位置**:`app/routers/pages.py:277`
- **本次变更**:新增 `"tokens": token_store.list_tokens(session)`,供设置页直接渲染令牌卡片

## `__all__`

- **位置**:`app/routers/pages.py:754`
- **本次变更**:补充导出 `timezone_groups`,方便测试与外部脚本引用清单

## 变更记录

- 2026-09-29:新增 `_timezone_context`;`COMMON_TIMEZONES` 改为清单派生;
  邮件队列上下文补充 `default_timezone`。
- 2026-09-29:新增远程接入令牌管理片段(`#agent-tokens`)与 `settings_page` 的 `tokens` 上下文。
