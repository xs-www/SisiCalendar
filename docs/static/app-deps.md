# `app/deps.py`

FastAPI 依赖与 Jinja2 模板环境。本次新增模板过滤器 `localtime`。

## 新增

### `local_time_filter(value, tz_name="UTC", fmt="%Y-%m-%d %H:%M") -> str`

- **位置**:`app/deps.py:33`,注册为模板过滤器 `localtime`(`app/deps.py:49`)
- **用途**:把库里的 **naive UTC** 时间按指定 IANA 时区渲染成给人看的时间
- **参数**:
  - `value`:naive UTC `datetime`;`None` 返回 `—`
  - `tz_name`:IANA 时区名,**非法时退回 UTC 而不抛错**(页面不该因为一条脏配置整页 500)
  - `fmt`:`strftime` 模板,列表页默认到分钟,`title` 属性用到秒
- **返回**:格式化字符串;缺失值为 `—`,与页面其它空值一致
- **模板用法**:
  ```jinja
  {{ delivery.created_at | localtime(default_timezone) }}
  {{ delivery.sent_at | localtime(default_timezone, '%Y-%m-%d %H:%M:%S') }}
  ```
- **不变量**:库内时间一律 naive UTC,换算统一走 `app.timeutil.utc_to_local`,
  不要在模板里做时区算术
- **测试**:`tests/test_timezones.py::test_mail_queue_shows_operation_and_sent_time`

## 变更记录

- 2026-09-29:新增 `local_time_filter` / `localtime` 过滤器,供邮件队列展示操作与发送时间。
