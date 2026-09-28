# `app/schemas.py`

Pydantic 输入/输出模型。本次变更集中在「让 `timezone` 字段收下 JSON 数字序号」。

## 函数

### `_timezone_before(value) -> Any`

- **位置**:`app/schemas.py:16`
- **用途**:`timezone` / `default_timezone` 的 `mode="before"` 校验器:把 JSON 里的数字
  (`1`)转成字符串 `"1"`,使 `"timezone": 1` 与 `"timezone": "1"` 行为一致
- **参数**:任何原始输入;`bool` 与 `None` 原样返回(避免 `True` 被当成序号 1)
- **返回**:字符串序号 / 原值
- **说明**:真正的语义解析在 `app.timezones.resolve_timezone`,这里只做类型归一
- **测试**:`tests/test_timezones.py::test_events_api_accepts_timezone_index`

## 模型字段

### `EventWrite.timezone`

- **位置**:`app/schemas.py:51`(校验器挂在 `app/schemas.py:77`)
- **类型**:`str | None`,默认 `None`;经 `_normalize_timezone` 预处理
- **接受**:IANA 名、中文名、序号(数字或数字字符串)
- **不变式**:模型层不校验时区合法性——非法值留给
  `app/events.py::normalize_event_input` 统一返回 422 字段错误

### `SettingsUpdate.default_timezone`

- **位置**:`app/schemas.py:137`(校验器挂在 `app/schemas.py:143`)
- **类型**:`str | None`,同样经 `_normalize_timezone`
- **校验**:`app/settings_store.py::validate_overrides` 负责解析与拒绝

## 变更记录

- 2026-09-29:新增 `_timezone_before`,挂到 `EventWrite.timezone` 与 `SettingsUpdate.default_timezone`。
