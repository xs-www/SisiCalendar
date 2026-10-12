# `app/timeutil.py`

时间换算工具。库内一律 naive UTC,输入按偏移量或事件时区解释,展示与 ICS 生成时再还原。

家庭版(2026-10-08)**没有**在本文件新增函数:账号时区、`build_range` 的「当地今天」
修正都复用已有助手——`app/accounts.py::user_timezone`(个人时区回落)、
`app/events.py::build_range` 里的 `utc_to_local` + `local_to_utc` 组合。
下面这一条是上一批(2026-10-06)新增、至今仍在本仓库生效的函数,它的唯一调用方是
`app/sync_service.py::input_time_text`。

## 函数

### `input_utc_offset(value) -> timedelta | None`

- **位置**:`app/timeutil.py:140`
- **用途**:取出调用方提交文本里**显式写明**的 UTC 偏移量
- **参数**:任意受 `normalize_datetime_text` 支持的写法;返回 `None` 表示「没写偏移量或解析失败」
- **接受**:`2026-11-03T14:00:00+11:00` → `timedelta(hours=11)`;`...+08:00`、`...-05:30` 同理;
  结尾 `Z`/`z` 走 `normalize_datetime_text` 归一成 `+00:00`,返回 **`timedelta(0)`(零偏移,不是 `None`)**
- **返回**:`None` 的两种情况必须区分:输入是 naive(如 `2026-11-03 14:00`)、或根本无法解析
- **副作用**:无
- **不变量**:只读不写,不改变任何落库语义;判断「有没有偏移量」请一律用 `is None`,
  **不要**用真值判断——`timedelta(0)` 是假值(踩过一次,见 `app/sync_service.py::input_time_text`)
- **测试**:`tests/test_mail.py::test_utc_offset_is_not_treated_as_missing`、
  `tests/test_mail.py::test_event_time_text_uses_submitted_offset`

## 变更记录

- 2026-10-06:新增 `input_utc_offset`,供邮件正文按「提交者当时的钟点」表述时间。
- 2026-10-08:家庭版归档 —— 本文件无新增函数;确认账号个人时区
  (`app/accounts.py::user_timezone`)与 `build_range` 的当地零点换算都建立在已有助手之上,
  `input_utc_offset` 的调用方仍只有 `app/sync_service.py::input_time_text`。