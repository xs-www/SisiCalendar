# `app/routers/api_settings.py`

应用设置 API(非敏感配置)。本次变更:读接口新增 `timezone_choices`。

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

### `update_settings(payload, ...) -> dict[str, object]`

- **位置**:`app/routers/api_settings.py:63`
- **本次变更**:无逻辑改动;`default_timezone` 现在可以提交序号(如 `1`),
  解析与拒绝都在 `app/settings_store.py::validate_overrides`
- **测试**:`test_settings_api_accepts_timezone_index`、
  `tests/test_api_settings.py::test_settings_update_and_validation`

## 相关接口

- `GET /api/v1/settings` 需要管理员会话;`PATCH` 还需要 CSRF 头。
- 事件接口的 `timezone` 序号解析在 `app/events.py`,与本接口共用 `app/timezones.py` 的清单。

## 变更记录

- 2026-09-29:读接口新增 `timezone_choices`;`default_timezone` 支持序号。
