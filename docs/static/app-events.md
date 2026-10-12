# `app/events.py`

事件领域逻辑:输入校验、查询、写入与重复事件展开。本次(2026-10-08)变更为
**多日历归属与范围过滤**,外加一处 `build_range` 的「当地今天」修正。

## 新增:多日历归属与范围过滤(2026-10-08)

### `create_event(session, payload, *, ..., calendar_id=None, user_id=None) -> Event`

- **位置**:`app/events.py:315`
- **本次变更**:新增两个关键字参数
  - `calendar_id`:事件**归属的日历**——可见性与写权限都从它推导
  - `user_id`:记录创建人,落到 `Event.created_by_user_id`
- **不变量**:**本函数不做授权**。调用方必须先做完权限判断
  (`CalendarAccess.can_write(calendar_id)`),不能指望这里兜底
- **副作用**:写库(`session.add`)
- **测试**:`tests/test_family_mail.py::test_agent_token_writes_into_its_bound_calendar`
  (断言返回的 `calendar_id` / `created_by_user_id`)、
  `tests/test_family_web.py::test_member_can_create_in_own_personal_calendar`

### `move_event_to_calendar(session, event, calendar_id) -> bool`

- **位置**:`app/events.py:348`
- **用途**:把事件挪到另一个日历
- **返回**:`True` 表示真的换了日历;**`event.calendar_id` 已经等于目标时返回 `False`**,
  不写库、不递增 `sequence`、不产生新版本(与「无实际变化不递增」一致)
- **副作用**:改 `calendar_id`、刷 `updated_at`、`sequence += 1`、`session.flush()`
- **不变量**:目标日历的**写权限由调用方判断**;换日历必须递增 `sequence`,
  否则订阅端不会重新拉取这条事件
- **测试**:`tests/test_family_web.py::test_admin_can_move_event_between_calendars`、
  `::test_admin_cannot_move_event_into_member_private_calendar`(越界目标 → 404,
  因为目标日历根本不在 `viewable_ids()` 里)

### `get_event(session, event_id, *, include_deleted=False, calendar_ids=None) -> Event | None`

- **位置**:`app/events.py:412`
- **本次变更**:新增 `calendar_ids` 范围过滤
- **语义**:
  - `None`(缺省)= 不过滤(导出、后台与运维脚本用)
  - 非空列表 = 只在该集合内查
  - **空列表 = 直接返回 `None`**,绝不退化成「不过滤」
- **返回**:不在可见范围内时返回 `None`,调用方应转成 **404**——越权读别人的私人日历
  既不命中也不暴露事件是否存在
- **测试**:`tests/test_family_accounts.py::test_member_cannot_see_owner_personal_calendar`、
  `::test_admin_cannot_see_member_personal_calendar`;
  页面侧 `tests/test_family_web.py::test_member_cannot_open_someone_elses_event_page`

### `query_events(session, ..., calendar_ids=None) -> tuple[list[Event], int]`

- **位置**:`app/events.py:505`
- **本次变更**:新增 `calendar_ids` 范围过滤,语义与 `get_event` 完全一致:
  `None` 不过滤、空列表直接 `return [], 0`
- **不变量**:过滤条件必须在**计数**与**取数**两侧同时生效,否则分页总数会泄露
  「存在多少条你看不见的事件」
- **测试**:`tests/test_family_accounts.py::test_events_are_isolated_per_calendar`
  (含 `query_events(session, calendar_ids=[])` 的零结果断言)、
  `tests/test_family_web.py::test_events_api_hides_other_peoples_personal_calendars`

### `serialize_event(event, *, access=None) -> dict[str, object]`

- **位置**:`app/events.py:749`
- **本次变更**:
  - 输出新增 `calendar_id` 与 `created_by_user_id`
  - 新增关键字参数 `access`(`app/accounts.py::CalendarAccess`):给出时**追加**四个键——
    `calendar_name`、`calendar_color`、`calendar_kind`、`can_write`;
    日历不在快照里时 `calendar_name` / `calendar_color` / `calendar_kind` 为 `None`,
    `can_write` 为假
  - 不给 `access` 时只输出事件自身字段(保持旧调用方行为不变)
- **用途**:界面据此给事件着色(`calendar_color`)、在 tooltip 里显示日历名,
  并由 `can_write` 决定是否允许拖动改期
- **测试**:`tests/test_family_web.py::test_editor_role_can_write_shared_calendar`
  (断言响应里的 `calendar_name`)

### `serialize_occurrence(occurrence, *, access=None)` / `serialize_events_with_occurrences(events, date_range, *, access=None)`

- **位置**:`app/events.py:813` / `app/events.py:846`
- **本次变更**:把 `access` 一路透传给 `serialize_event`;其余行为不变
  (`date_range is None` 时走扁平序列化,否则展开重复实例再按开始时间排序)

### `build_range(start, end, *, settings=None, default_days=...) -> DateRange`(真实 bug 修正)

- **位置**:`app/events.py:448`
- **本次变更**:缺省起点原先用 **`utcnow().date()`(UTC 的今天)** 去套当地零点,现在改成
  先用 `utc_to_local(utcnow(), tz_name).date()` 得到**应用默认时区的今天**,再
  `local_to_utc(...)` 换算成当地零点
- **为什么是 bug**:当地刚过午夜时,UTC 的日期还停在**前一天**(UTC+10 时区里,
  当地 00:30 对应的 UTC 时刻是前一天 14:30)。旧写法把这个「UTC 的昨天」当成本地日期去
  套当地零点,窗口起点就退到了**当地昨天 00:00**,于是昨天的全天事件重新落进窗口、
  再次出现在邮件附件里;UTC-5 之类的时区则相反,会把当天的一部分事件漏在窗口外
- **不变量**:`end` 仍是不含边界;`start`/`end` 显式给出时行为完全不变;
  仅影响「缺省起点」这一个分支,发布文件与版本快照不受影响(不变量 3 仍成立)
- **测试**:**没有找到专门覆盖这个时区边界的用例**。现有相关覆盖是
  `tests/test_mail.py::test_attachment_keeps_all_day_events_of_today`(用 `utc_to_local`
  算出今天的全天事件必须保留)与 `::test_attachment_drops_events_that_ended_today`,
  都不构造「当地刚过午夜」的时刻——这里需要补一个 UTC+10 的用例
  (`tests/conftest.py` 的 `TEST_TIMEZONE` 已经是 `Australia/Brisbane`)

## 变更的函数

### `normalize_event_input(payload, *, existing=None, settings=None) -> dict[str, object]`

- **位置**:`app/events.py:101`(时区分支在 `app/events.py:149` 附近)
- **本次变更**:
  - 提交了 `timezone` 时改走 `app.timezones.resolve_timezone`,不再直接 `strip()`:
    现在接受**序号**(`1` / `"1"`)、**中文名**(`北京时间`)、下拉框整行文本
    (`11. 悉尼时间(Australia/Sydney)`)与 IANA 名
  - 解析失败时用 `timezone_help()` 生成提示(写明可填序号与区间),并回退到默认时区继续
    校验其它字段,保证一次性返回所有字段错误
  - 未提交 `timezone` 时行为不变:沿用既有事件的时区,或 `settings.default_timezone`
- **副作用**:无(仅计算待写入字典)
- **不变量**:落库的 `timezone` 永远是合法 IANA 名;不带偏移量的输入时间仍按该时区解释
  (见 `tests/test_api_events.py::test_naive_input_uses_event_timezone`)
- **测试**:`tests/test_timezones.py::test_events_api_accepts_timezone_index`、
  `::test_agent_api_accepts_timezone_index`、`::test_agent_api_accepts_chinese_timezone_label`、
  `::test_invalid_timezone_error_mentions_index`、`::test_event_without_timezone_uses_settings_default`

## 影响面

- `create_event`、`update_event` 通过 `normalize_event_input` 间接获得新能力,自身无改动。
- 远程 Agent 接口(`app/routers/api_agent.py`)与 `/api/v1/events` 走同一条路径,
  因此表单形态(`application/x-www-form-urlencoded`)也能填 `timezone=1`。
- 邮件正文里的「事件时间」描述(`app/sync_service.py::event_time_text`)只读事件的
  `timezone`,因此自动跟着变化,无需改动。
  (2026-10-06 补充:调用方提交的时间文本**带偏移量**时会改按该偏移表述,见
  [`app-sync-service.md`](app-sync-service.md);本函数的行为未受影响。)
- **2026-10-08 补充**:权限过滤**不是**默认开启的——`get_event` / `query_events` 的
  `calendar_ids` 缺省都是 `None`(不过滤),`app/exporter.py::active_events` /
  `::upcoming_events` 同理。这是给导出与运维脚本留的口子。**所有面向用户的读路径都必须
  显式传 `CalendarAccess.viewable_ids()`**;漏传就等于把别人的私人日历暴露出去。

## 新增:提醒(`reminders`)与链接(`url`)

### `normalize_event_input(payload, *, existing=None, settings=None)` 的两个新字段

- **位置**:`app/events.py:147`(`url`)、`app/events.py:156`(`reminders`);`MAX_URL` 在 `:59`
- **行为**:
  - `url`:去空白;空串视为「清除」→ 落库 `None`;超过 `MAX_URL`(1000)→ 字段错误;
    **不提交该键时沿用既有值**(与 title/location 同构)
  - `reminders`:交给 `app/reminders.py::normalize_reminders`,得到去重且**从大到小**排序的
    列表,再用 `dump_reminders` 存成 JSON 文本;非法值写 `errors["reminders"]` 并**沿用既有值**
    (保证一次性返回所有字段错误);不提交该键时沿用既有值
  - 返回值字典新增 `"url"` 与 `"reminders"`,因此 `update_event` 的变化比较**天然覆盖**它们:
    提醒变了才递增 `sequence`
- **参数**:`reminders` 在模型层已归一为 `list[int] | None`(表单字符串由
  `app/schemas.py::_reminders_before` 解析)
- **不变量**:落库的 JSON 顺序只取决于集合本身,见 `app/reminders.py` 的说明
- **测试**:`tests/test_reminders.py::test_patch_without_reminders_keeps_them_and_does_not_bump_version`、
  `::test_patch_can_clear_reminders`、`::test_update_event_reminder_change_bumps_sequence`、
  `::test_api_rejects_invalid_reminders`

### `serialize_event(event) -> dict[str, object]` 的提醒与链接输出

- **位置**:`app/events.py:749`
- **本次变更**:输出新增
  - `url`(或 `null`)
  - `reminders`:分钟数列表,如 `[1440, 15, 0]`
  - `reminder_labels`:对应中文描述,如 `["提前 1 天", "提前 15 分钟", "事件开始时"]`
- **用途**:界面 tooltip 与外部脚本都不必自己拼描述
- **测试**:`tests/test_reminders.py::test_api_create_and_read_reminders`

## 变更记录

- 2026-09-29:时区字段改走 `resolve_timezone`,支持序号与中文名;错误文案改为 `timezone_help()`。
- 2026-09-30:新增 `url` 与 `reminders` 字段的校验与序列化(`MAX_URL = 1000`)。
- 2026-10-08:家庭版多日历 —— `create_event` 新增 `calendar_id`/`user_id`,
  新增 `move_event_to_calendar`;`get_event`/`query_events` 支持 `calendar_ids` 范围过滤
  (空列表返回空、绝不退化成不过滤);`serialize_event`/`serialize_occurrence`/
  `serialize_events_with_occurrences` 支持 `access`(日历名、颜色、`can_write`);
  修正 `build_range` 缺省起点:由 **UTC 的今天**改为**应用默认时区的今天**,
  避免 UTC+10 时区当地刚过午夜时把「昨天」的全天事件重新塞进邮件附件。
