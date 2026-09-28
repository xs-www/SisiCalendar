# `app/settings_store.py`

非敏感配置覆盖项的读写。本次变更:`default_timezone` 覆盖项支持序号 / 中文名。

## 变更的函数

### `validate_overrides(values) -> dict[str, str]`

- **位置**:`app/settings_store.py:49`(时区分支在 `app/settings_store.py:65` 附近)
- **本次变更**:`default_timezone` 分支改走 `app.timezones.resolve_timezone`
  - 输入 `1`、`"1"`、`"北京时间"`、`"Asia/Shanghai"`、`"1. 北京时间(Asia/Shanghai)"` 都写入
    解析后的 IANA 名
  - 无法识别时抛 `SettingValidationError`,字段错误文案来自 `timezone_help()`
- **返回**:清洗后的键值字典(值为字符串),`default_timezone` 为规范 IANA 名
- **副作用**:无(写库由 `save_overrides` 负责)
- **测试**:`tests/test_timezones.py::test_settings_api_accepts_timezone_index`、
  `tests/test_api_settings.py::test_settings_update_and_validation`(非法值仍 422)、
  `::test_settings_form_partial_updates_and_validates`(表单直接提交 IANA 名仍可用)

### `effective_settings(session, base=None) -> Settings`

- **位置**:`app/settings_store.py:115`
- **本次变更**:无行为变化;仍会用 `is_valid_timezone` 兜底校验覆盖值,
  因此历史数据里若有脏时区会被丢弃并退回环境配置

## 相关

- 设置页下拉框的「当前值不在常用清单中」分支依赖 `app.routers.pages._timezone_context`
  传入的 `timezone_known`(见 `docs/static/app-routers-pages.md`)。

## 变更记录

- 2026-09-29:`default_timezone` 支持序号与中文名。
