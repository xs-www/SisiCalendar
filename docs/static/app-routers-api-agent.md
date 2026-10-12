# `app/routers/api_agent.py`

远程 Agent 接入接口(前缀 `/api/agent`)。用途:让本机之外的自动化 Agent(脚本、LLM Agent、
iOS 快捷指令等)通过 HTTP 新建日程。

设计原则:

- **默认关闭**:没有任何令牌时整个 `/api/agent` 返回 404,等同不存在
  (判定在 `app/deps.py::require_agent_token`,见 [`app-deps.md`](app-deps.md));
- **只认 Bearer 令牌**:不接受 Cookie 会话,因此没有 CSRF 面,已登录会话也调不动;
- **令牌带作用域**:每枚令牌绑定一个账号与可选的默认日历,权限按该账号判定;
- **请求体宽容**:同时接受 JSON 与表单;
- **幂等**:`client_ref` 让重试安全。

## 常量

| 符号 | 位置 | 说明 |
|---|---|---|
| `MAX_CLIENT_REF` | `app/routers/api_agent.py:48` | 幂等键长度上限 200 字符 |
| `FORM_CONTENT_TYPES` | `:49` | `application/x-www-form-urlencoded` 与 `multipart/form-data` |

## 辅助函数

### `_serialize_result(event, *, duplicate, sync) -> dict`

- **位置**:`app/routers/api_agent.py:52`
- **用途**:事件序列化 + `duplicate` 标志 + `sync` 结果
- **返回**:**不带 `access`** 的 `serialize_event`(因此没有 `can_write` 等权限字段,
  这些字段走默认值 `False`/`None`)
- **不变量**:响应里绝不出现令牌明文(测试直接断言明文不在响应体中)

### `read_event_payload(request) -> EventWrite`

- **位置**:`app/routers/api_agent.py:59`(异步)
- **用途**:解析请求体为 `EventWrite`
- **参数语义**:
  - `application/json`(或 `*+json`):必须是 JSON **对象**,否则 422
  - 表单:`空字符串一律视为「未提供」`,快捷指令里留空的字段不会被当成有效值
  - 其他 content-type → **415** `unsupported_media_type`
- **状态码**:**415** 不支持的媒体类型;**422** JSON 非法 / 不是对象 / 字段校验失败
  (带 `field_errors_from` 的字段级错误)
- **副作用**:无(只读请求体)
- **测试**:`tests/test_api_agent.py::test_accepts_form_encoded_body_for_shortcuts`、
  `::test_unsupported_content_type_rejected`、`::test_malformed_json_is_reported`、
  `::test_unknown_form_field_is_rejected`、`::test_validation_errors_are_structured`

### `_token_calendar_id(principal, payload, access, session) -> str | None`

- **位置**:`app/routers/api_agent.py:102`
- **用途**:决定新建事件落哪条日历,优先级为
  **令牌固定日历 → 请求体 `calendar_id` → 该账号的默认可写日历**
- **参数**:`principal.calendar_id` 为空表示「用账号个人的默认可写日历」
- **状态码**:候选日历对当前令牌**不可见 → 404**(「日历不存在或令牌无权访问」);
  可见但**不可写 → 403**(「令牌绑定的账号没有修改该日历的权限」)
- **返回**:日历 id;`None` 表示该账号一条可写日历都没有(调用方回 **409**)
- **不变量**:令牌的 `calendar_id` 只是「默认落库位置」,**不是**额外授权——
  成员令牌指向共享日历时,若该成员只是 `viewer` 依然 403
- **测试**:`tests/test_family_mail.py::test_agent_token_writes_into_its_bound_calendar`、
  `::test_agent_token_without_write_permission_is_rejected`、
  `::test_editor_token_can_write_shared_calendar`

## 路由契约

### `POST /api/agent/events` — `agent_create_event`

- **位置**:`app/routers/api_agent.py:137`(异步);依赖 `AgentDep`(Bearer 令牌)
- **状态码与顺序**:
  1. 令牌来源全空 / 令牌无效 → **404** / **401**(由 `require_agent_token` 决定)
  2. 超出限流(键 = `agent:<指纹>`,窗口 60 秒,上限取
     `effective_settings.agent_rate_limit_per_minute`)→ **429** + `Retry-After`
  3. 令牌绑定的账号不存在或已停用 → **401** `unauthorized`(审计 `agent_create/no_owner`)
  4. 请求体解析失败 → **415** / **422**
  5. `client_ref` 超过 200 字符 → **422**(字段错误 `client_ref`)
  6. `client_ref` 命中**别人账号**的事件 → **409** `conflict`(见下)
  7. 没有任何可写日历 → **409** `no_writable_calendar`
  8. 事件校验失败 → **422** `validation_error`
  9. **新建成功 → 201**
- **成功行为**:`create_event(source="agent", client_ref=..., calendar_id=..., user_id=令牌账号)`
  → `mark_export_dirty` → 提交 → `publish(CHANGE_CREATED)`;
  审计 detail 带 `event.id version calendar=`;邮件正文时间按提交者偏移表述
- **副作用**:落库、生成 ICS、为启用收件人建发信任务(与界面同一条链路)
- **不变量**:写入权限完全按令牌绑定账号的 `CalendarAccess` 判定
- **测试**:`tests/test_api_agent.py::test_agent_creates_event_and_syncs`、
  `::test_rate_limit`、`::test_agent_event_editable_from_admin_ui`、
  `tests/test_family_mail.py::test_agent_token_of_disabled_account_is_rejected`、
  `::test_legacy_env_agent_token_belongs_to_owner`

#### `client_ref` 幂等边界(必须保持)

- **同账号重放**:同一 `client_ref` 已存在且该事件对当前令牌**可见**时,
  **原样返回**既有事件(HTTP **200**、`duplicate: true`、`sync: null`);
  **不得新建、不得重新导出、不得发信**。
- **跨账号重放**:若该 `client_ref` 命中一条对当前令牌**不可见**的事件 →
  **409** `conflict`,文案只说「该 client_ref 已被其他账号使用」,
  响应体里**不含**对方的事件标题或 id。这样既阻止跨账号污染幂等键,也不泄露内容。
- **测试**:`tests/test_api_agent.py::test_client_ref_makes_retry_idempotent`、
  `::test_different_client_ref_creates_separate_events`、
  `tests/test_family_mail.py::test_agent_client_ref_is_scoped_per_account`

### `GET /api/agent/events/{event_id}` — `agent_read_event`

- **位置**:`app/routers/api_agent.py:263`;依赖 `AgentDep`
- **用途**:只读回查,便于 Agent 在超时后确认结果
- **状态码**:**401** 令牌绑定账号不可用;**404** 事件不存在、不在**该令牌可见范围**内、
  或**不是 Agent 来源**(Web 创建的事件不通过本接口暴露)
- **返回**:事件序列化 + `duplicate: false` + `sync: null`
- **不变量**:跨账号读必须 404(不区分「不存在」与「无权限」)
- **测试**:`tests/test_api_agent.py::test_agent_read_back_own_event`、
  `::test_agent_cannot_read_web_created_event`、
  `tests/test_family_mail.py::test_agent_token_cannot_read_another_users_event`

## 关键不变量(汇总)

1. `/api/agent` 只认 `Authorization: Bearer`;Cookie 会话无效。
2. 两个令牌来源都没有时整段 **404**(不是 401),避免暴露接口存在。
3. 令牌明文只出现在创建响应里一次;Agent 响应与日志都只有指纹。
4. `client_ref` 命中(且可见)时必须原样返回既有事件,不得产生任何副作用。
5. 新建事件的归属账号 = 令牌的 `user_id`,落库日历 = 令牌 `calendar_id` →
   请求体 `calendar_id` → 账号默认写日历。
6. 事件可见性、可写性全部来自 `app/accounts.py::CalendarAccess`。

## 找不到对应测试的点

- 「没有任何可写日历 → 409 `no_writable_calendar`」没有专门测试。
- 「请求体显式 `calendar_id` 不可见 → 404」在 Agent 路径没有单独测试
  (有等价的事件 API 侧覆盖)。
- 令牌 `calendar_id` 指向**已删除**日历时的行为没有专门测试。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档令牌 → 账号 → 可见/可写范围的解析、
  `client_ref` 命中时仅当对当前令牌可见才原样返回(否则 409)、
  新建事件落到「令牌固定日历 → 请求体日历 → 账号默认写日历」、
  以及跨账号读 404 与禁用账号 401。