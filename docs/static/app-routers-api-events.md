# `app/routers/api_events.py`

事件 REST API(`/api/v1/...`)。家庭版起**可见性由日历决定**:

- 读不到的事件返回 **404**(不暴露存在性);
- 写不动的事件返回 **403**(明确告诉调用方是权限问题);
- 私人日历只有归属账号可读可写,**管理员也不行**。

权限判定一律经 `app/accounts.py::calendar_access`,本模块不自行拼 SQL 判断。

## 主编排函数

### `_resolve_write_calendar(payload_calendar_id, access, *, existing_event_calendar_id=None) -> str`

- **位置**:`app/routers/api_events.py:51`
- **用途**:决定这次写入落到哪条日历
- **规则(按顺序)**:
  1. 请求体显式给了 `calendar_id` → 先 `can_view`(否则 **404**「日历不存在或你没有访问权限」),
     再 `can_write`(否则 **403**);通过则用它
  2. 修改既有事件且没给日历 → 沿用原日历,但必须可写(否则 **403**)
  3. 新建且没给日历 → `access.default_write_calendar_id()`(优先个人日历,其次第一条可写的共享日历);
     一条可写的都没有 → **409** `no_writable_calendar`
- **返回**:目标日历 id(非空)
- **不变量**:显式指定不可写日历必须 403,不能悄悄回落到默认可写日历
- **测试**:`tests/test_family_web.py::test_member_cannot_write_to_shared_calendar`、
  `::test_member_can_create_in_own_personal_calendar`、
  `::test_editor_role_can_write_shared_calendar`

### `_validation_error(exc) -> HTTPException`

- **位置**:`app/routers/api_events.py:44`
- **用途**:把 `EventValidationError.fields` 包成 **422** 结构化错误
- **副作用**:无(构造异常)

## 路由契约

### `GET /api/v1/calendars` — `list_calendars`(**新增**)

- **位置**:`app/routers/api_events.py:95`;依赖 `UserDep`(登录即可,不必管理员)
- **返回**:
  ```json
  {
    "items": [{"id", "name", "kind", "color", "role", "can_write", "owner_user_id"}, ...],
    "default_calendar_id": "<新建事件的默认日历>"
  }
  ```
  - `kind`:`personal` / `shared`
  - `role`:`owner` / `editor` / `viewer`(个人日历恒为 `owner`)
  - `can_write`:写权限来自角色,共享日历新成员默认 `viewer`(不可写)
- **不变量**:`items` 只包含调用者可见的日历——别人的私人日历**根本不出现**
- **测试**:`tests/test_family_web.py::test_calendars_endpoint_lists_visible_calendars_with_roles`、
  `tests/test_family_accounts.py::test_member_cannot_see_owner_personal_calendar`、
  `::test_admin_cannot_see_member_personal_calendar`

### `GET /api/v1/events` — `list_events`

- **位置**:`app/routers/api_events.py:115`;依赖 `UserDep`
- **查询参数**:`start` / `end` / `q` / `status` / `source` / `all_day` / `recurring` /
  `calendar_id`(可重复)/ `expand`(默认 `true`)/ `limit`(≤2000)/ `offset`
- **范围语义**:
  - 基准集合是 `access.viewable_ids()`(**私人日历只有自己的**);
  - 传了 `calendar_id` 时**取交集**:只保留调用方真正有权看的 id,
    越权 id **直接忽略而不是报错**(避免泄露存在性,也不会把 `[]` 变成错误)
  - 只有显式给了 `start`/`end` 才做日期过滤,否则关键词检索覆盖全部事件(不变量 10)
- **返回**:`{"items", "total", "calendars", "range"}`;`calendars` 是调用者可见的日历 id 列表
- **测试**:`tests/test_family_web.py::test_events_api_hides_other_peoples_personal_calendars`、
  `::test_events_api_requires_login`、`tests/test_api_events.py::test_list_events_expands_range`、
  `::test_list_events_keyword_and_status_filter`

### `POST /api/v1/events` — `create_event_endpoint`

- **位置**:`app/routers/api_events.py:191`;依赖 `CsrfHeaderDep`(**登录 + `X-CSRF-Token`**)
- **状态码**:**201** 成功;**403** 目标日历不可写;**404** 目标日历不可见;
  **409** 一条可写日历都没有;**422** 事件校验失败
- **行为**:`_resolve_write_calendar` → `create_event(...)`(带 `calendar_id` 与
  `user_id=current.user_id`)→ `mark_export_dirty` → 提交 → `publish` 生成 ICS 与邮件任务
  (显式传 `changed_calendar_ids=[目标日历]`,只有看得见这条日历的收件人才收信)
- **返回**:序列化事件 + `sync` 结果
- **测试**:`tests/test_family_web.py::test_member_can_create_in_own_personal_calendar`、
  `::test_member_cannot_write_to_shared_calendar`、
  `tests/test_api_events.py::test_mutation_requires_csrf`

### `GET /api/v1/events/{event_id}` — `read_event`

- **位置**:`app/routers/api_events.py:231`;依赖 `UserDep`
- **状态码**:**200**;**404** 不存在、已删除或**不在可见范围内**(三者不区分)
- **返回**:序列化事件 + `occurrences`(未来 365 天内的实例)
- **不变量**:管理员读别人的私人日程同样 404
- **测试**:`tests/test_family_web.py::test_admin_cannot_read_member_personal_event`、
  `::test_member_cannot_edit_other_peoples_private_event`(读侧同源 404)

### `PATCH /api/v1/events/{event_id}` — `update_event_endpoint`

- **位置**:`app/routers/api_events.py:253`;依赖 `CsrfHeaderDep`
- **状态码**:**200**;**403** 事件所在日历不可写;**404** 事件不在可见范围;
  **409** 乐观锁冲突(`expected_updated_at` 不匹配);**422** 字段校验失败
- **移动事件**:请求体里的 `calendar_id` 与当前不同时,过 `_resolve_write_calendar`
  (目标必须可见且可写),再 `app/events.py::move_event_to_calendar`;
  把事件移进**别人的私人日历** → 404(不可见),移进自己不可写的共享日历 → 403
- **移动的顺序(2026-10-12 第四轮修正)**:先 `update_event`(内含乐观锁校验),
  **再**搬家。`move_event_to_calendar` 会刷新 `updated_at`,此前先搬家会让
  「客户端看到的那一版」立刻变成过期版本——凡是从网页/接口带着
  `expected_updated_at` 换日历的请求**必然 409**,搬家功能实际上是坏的。
  搬家失败(403/404)时抛出的异常不会带上已经写入的字段改动(请求会话不提交)。
- **发信范围**:调 `publish(..., changed_calendar_ids=[旧日历, 新日历])`
  —— 事件在两边的可见者都要被告知,单传新日历会让原日历的读者以为事件凭空消失
- **无变更短路**:既没改字段也没移动时不递增 `sequence`、不产生新版本、
  不触发导出与发信(`SyncOutcome(changed=False)`)
- **测试**:`tests/test_family_web.py::test_admin_can_move_event_between_calendars`、
  `::test_admin_cannot_move_event_into_member_private_calendar`、
  `::test_member_cannot_edit_or_delete_shared_event`、
  `tests/test_api_events.py::test_optimistic_lock_conflict`、
  `::test_noop_update_does_not_create_new_version`、
  `tests/test_account_recipient_sync.py::test_event_move_hands_both_calendars_to_publish`
  (搬家时新旧两条日历都交给 `publish`;带回 `expected_updated_at` 的搬家必须 200)

### `DELETE /api/v1/events/{event_id}` — `delete_event_endpoint`

- **位置**:`app/routers/api_events.py:318`;依赖 `CsrfHeaderDep`
- **状态码**:**200**;**403** 不可写;**404** 不可见/不存在
- **行为**:**软删除**(保留行供审计),`mark_export_dirty` → 提交 →
  `publish(CHANGE_DELETED, changed_calendar_ids=[事件所在日历])`
- **返回**:`{"ok": true, "id": ..., "sync": {...}}`
- **测试**:`tests/test_family_web.py::test_member_cannot_edit_or_delete_shared_event`、
  `tests/test_api_events.py::test_delete_is_soft_and_removes_from_ics`、
  `::test_deleted_event_keeps_row_for_audit`

## 状态码总表

| 情况 | 读(列表/单条) | 写(建/改/删) |
|---|---|---|
| 未登录 | 401 | 401 |
| 已登录但缺/错 CSRF 头 | 不适用(只读不校验 CSRF) | 403 |
| 事件或日历**不在可见范围** | 404 | 404(建事件时目标日历不可见) |
| 事件/日历可见但**不可写** | — | 403 |
| 没有任何可写日历 | — | 409 `no_writable_calendar` |
| 校验失败 / 乐观锁冲突 | — | 422 / 409 |

## 找不到对应测试的点

- 「一条可写日历都没有 → 409 `no_writable_calendar`」没有专门测试。
- 「显式 `calendar_id` 指向别人的私人日历 → 404」有间接覆盖
  (`test_admin_cannot_move_event_into_member_private_calendar`),但新建路径没单独测。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档新增的 `GET /api/v1/calendars`、
  `/api/v1/events` 只返回可见日历内事件且显式 `calendar_id` 取交集不报错、
  写操作按可写范围判定(显式不可写 403 / 越界 404)、PATCH 支持移动事件到别的日历。
- 2026-10-12(第四轮):三个写路由把**改动的日历**交给 `publish`
  (建/删传事件所在日历,PATCH 传新旧两条),让邮件只发给看得见这次改动的人;
  顺带修掉「先搬家再校验乐观锁」的顺序缺陷——搬家会刷新 `updated_at`,
  带 `expected_updated_at` 的换日历请求此前必然 409。