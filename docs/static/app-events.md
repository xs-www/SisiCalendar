# `app/events.py`

事件领域逻辑:输入校验、查询、写入与重复事件展开。本次变更只涉及时区解析。

## 变更的函数

### `normalize_event_input(payload, *, existing=None, settings=None) -> dict[str, object]`

- **位置**:`app/events.py:90`(时区分支在 `app/events.py:149` 附近)
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

## 变更记录

- 2026-09-29:时区字段改走 `resolve_timezone`,支持序号与中文名;错误文案改为 `timezone_help()`。
