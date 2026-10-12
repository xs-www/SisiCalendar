# `app/ics.py`

RFC 5545 导出。本次变更:事件新增**提醒**(`VALARM`)与**链接**(`URL`)两个导出字段。

## 新增

### `_build_valarm(event, minutes) -> Alarm`

- **位置**:`app/ics.py:67`
- **用途**:把一条提醒转成 `VALARM`——相对事件开始时间的 `DISPLAY` 提醒
- **参数**:`minutes` 为**提前分钟数**(见 `app/reminders.py`);`0` 表示事件开始时,
  函数内部取负得到 RFC 5545 的相对 `TRIGGER`
- **组件内容**:
  - `ACTION:DISPLAY`
  - `DESCRIPTION`:事件标题(为空时回退「日程提醒」)
  - `TRIGGER`:`DURATION` 形态,如 `15` → `-PT15M`、`1440` → `-P1D`、`0` → `P0D`
- **不变量**:
  - **只用相对时长**,绝不使用绝对时间:重复事件与跨时区/夏令时事件都由订阅端按各自实例
    的开始时间计算,不需要逐个实例生成提醒;
  - 全天事件的基准是当天 `00:00`,因此「提前 1 天」即前一天零点(与 iCloud 语义一致);
  - 组件内容只取决于数据库状态与事件标题,不含挂钟时间,保证导出哈希稳定
- **测试**:`tests/test_reminders.py::test_reminders_export_as_valarm`、
  `::test_all_day_reminder_uses_midnight_anchor`、`::test_reminders_survive_timezone_export`

### `_build_vevent(...)` 中的 `URL`

- **位置**:`app/ics.py:89`(写入点)、`app/ics.py:121`(VALARM 追加点)
- **本次变更**:
  - `event.url` 非空时输出 `URL` 属性(备注里的链接,对应 iPhone 日历的「URL」字段)
  - 追加完 `RRULE` 后,按 `serialize_reminders(event.reminders)` 的顺序**逐个**
    `add_component(_build_valarm(...))`
- **参数**:`minutes` 列表已由 `app/reminders.py` 去重并按从大到小排序
- **不变式**:子组件的**顺序**必须与列表顺序一致;顺序不依赖挂钟时间或字典迭代顺序
- **测试**:`tests/test_reminders.py::test_multiple_reminders_are_exported_newest_first`、
  `::test_reminder_export_is_independent_of_input_order`、
  `::test_url_is_exported_and_folded`

## 未变但相关的约束

- `DTSTAMP` / `LAST-MODIFIED` 仍取事件自身的 `updated_at`,提醒不改变这一点
  (见 `tests/test_ics.py::test_content_is_deterministic_across_wall_clock`)。
- 无提醒、无链接的事件导出结果与升级前**逐字节一致**,因此本次升级不会让已有日历重发。

## 变更记录

- 2026-09-30:新增 `_build_valarm`;`_build_vevent` 输出 `URL` 并追加 `VALARM`。
