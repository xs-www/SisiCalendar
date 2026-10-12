# `app/reminders.py`(新增)

日程提醒(ICS `VALARM`)的取值、预设与中文描述。本模块是「提醒」语义的**单一事实来源**,
被 `app/events.py`(校验/序列化)、`app/ics.py`(生成 `VALARM`)、`app/routers/pages.py`
(表单勾选项)共用。

## 取值语义

`reminders` 是**提前分钟数**的列表:

| 值 | 含义 |
|---|---|
| `0` | 事件开始时 |
| 正数 | 提前,如 `15` = 提前 15 分钟 |
| 负数 | 延后,如 `-10` = 开始后 10 分钟 |
| `1440` | 提前 1 天;全天事件以**当天 00:00** 为基准,故等于前一天零点 |

入库形态是 JSON 文本(如 `"[1440, 15, 0]"`),空表示不提醒,统一存 `NULL`。

## 常量

### `MAX_REMINDER_MINUTES = 43200`

- **位置**:`app/reminders.py:28`
- **用途**:单个提醒偏移的绝对值上限(30 天),同时挡住把秒当分钟写错的输入
- **不变式**:`validate_reminder` 对越界值一律返回 `None`;导出侧不再二次判断

### `PRESET_REMINDERS = (0, 5, 15, 30, 60, 120, 1440, 2880, 10080)`

- **位置**:`app/reminders.py:31`
- **用途**:表单里的常用提醒勾选项,顺序即界面顺序
- **不变式**:序号**不是**对外契约(与 `TIMEZONE_CHOICES` 不同),可以调整顺序或增删;
  但一旦调整,`preset_options` 的渲染顺序与 `custom_reminder_text` 的过滤集合要一致

### `SEPARATOR_CODEPOINTS = (",", ",", ";", ";")`

- **位置**:`app/reminders.py:34`
- **用途**:自定义提醒文本接受的分隔符(半角/全角逗号与分号;空白本来就分行)
- **说明**:写成码点元组而不是字典字面量,因为半角与全角字符在源码里视觉相同,
  `ruff` 的 F601 会把它们判成重复键

## 函数

### `describe_reminder(minutes) -> str`

- **位置**:`app/reminders.py:38`
- **用途**:把偏移量说成人话:`15` → 「提前 15 分钟」、`-1440` → 「延后 1 天」、`0` → 「事件开始时」
- **参数**:整数分钟数;越界或非整数返回「无效提醒」而不是抛异常(界面渲染不应因脏数据失败)
- **返回**:中文描述;**优先级为周 > 天 > 小时 > 分钟**(`90` → 「提前 90 分钟」)
- **测试**:`tests/test_reminders.py::test_describe_reminder`、`::test_describe_reminder_rejects_out_of_range`

### `preset_choices() -> tuple[tuple[int, str], ...]`

- **位置**:`app/reminders.py:56`
- **用途**:给脚本用的「(分钟数, 中文标签)」序列
- **返回**:按 `PRESET_REMINDERS` 顺序的元组,长度与之一致

### `preset_options(minutes) -> list[dict[str, Any]]`

- **位置**:`app/reminders.py:64`
- **用途**:表单勾选项:`[{"minutes": 15, "label": "提前 15 分钟", "selected": True}, ...]`
- **参数**:`minutes` 为当前事件的提醒列表(来自 `serialize_reminders`)
- **返回**:固定长度列表;`selected` 表示该预设命中当前事件
- **不变量**:自定义值(如 `45`)**不会**凭空多出一个勾选项,只出现在自定义输入框
- **测试**:`tests/test_reminders.py::test_preset_options_marks_selection`

### `custom_reminder_text(minutes) -> str`

- **位置**:`app/reminders.py:77`
- **用途**:把**非预设**的提醒回填到「自定义分钟」输入框,避免同一个值在两处重复显示
- **测试**:`tests/test_reminders.py::test_edit_form_marks_existing_reminders`

### `validate_reminder(value) -> int | None`

- **位置**:`app/reminders.py:83`
- **用途**:单个值的类型归一与范围判断;接受 `int`、整数值 `float`、数字字符串
- **返回**:合法分钟数;`None` 代表非法(`bool` 与 `None` 一律非法,避免 `True` 被当成 1)

### `parse_reminder_text(text) -> list[int]`

- **位置**:`app/reminders.py:108`
- **用途**:解析 `"15, 1440"` 这类表单文本;按 `SEPARATOR_CODEPOINTS` 与空白切分
- **返回**:解析出的分钟数列表;空片段(多余逗号)忽略,**非法片段同样被忽略**
- **说明**:严格校验不在这里——`app/schemas.py::_reminders_before` 与
  `normalize_reminders` 会先判非法并报错,因此本函数可以假定片段合法
- **测试**:`tests/test_reminders.py::test_parse_reminder_text_accepts_chinese_separators`

### `normalize_reminders(value) -> tuple[list[int] | None, str | None]`

- **位置**:`app/reminders.py:125`
- **用途**:把列表 / JSON 字符串 / 逗号分隔字符串归一成待入库列表
- **返回**:`(提醒列表, 错误文案)`;`(None, 文案)` 表示有非法值,`([], None)` 表示不提醒
- **不变量**:**去重后按从大到小排序**。顺序必须只取决于集合本身——导出器用内容哈希
  判断是否需要新版本,顺序随输入漂移会导致没有改动的保存也重发整份日历
- **测试**:`tests/test_reminders.py::test_normalize_reminders_is_order_and_duplicate_insensitive`、
  `::test_normalize_reminders_reports_invalid_values`

### `serialize_reminders(raw) -> list[int]`

- **位置**:`app/reminders.py:176`
- **用途**:读取数据库里的 JSON 文本,坏数据(非法 JSON、越界值)一律当空列表
- **返回**:干净的分钟数列表,永不为 `None`
- **测试**:`tests/test_reminders.py::test_serialize_reminders_tolerates_corrupt_data`

### `dump_reminders(minutes) -> str | None`

- **位置**:`app/reminders.py:188`
- **用途**:入库形态;空列表返回 `None`,避免「`NULL`」与「`[]`」两种空表示
- **不变式**:入库前必须已经过 `normalize_reminders`(已排序去重)

### `reminder_labels(minutes) -> list[str]`

- **位置**:`app/reminders.py:193`
- **用途**:批量中文描述,顺序与入参一致,供 `/api/v1/events` 的 `reminder_labels` 使用

## 影响面

- `app/ics.py::_build_valarm`:每条提醒生成一个 `VALARM`(`ACTION:DISPLAY` +
  `DESCRIPTION:标题` + `TRIGGER:-PT15M`),顺序与列表一致。
- 提醒用**相对时长**表达,因此重复事件与跨时区/夏令时事件都不需要逐实例换算。
- `app/schemas.py::EventWrite.reminders` 收下 JSON 数组或表单字符串;`EventRead`
  输出 `reminders` 与 `reminder_labels`。

## 变更记录

- 2026-09-30:新增模块。`events.reminders` 列 + `0004_event_reminders` 迁移
  (`reminders TEXT NULL`、`url VARCHAR(1000) NULL`,两列都可空);
  ICS 导出 `VALARM`;表单新增提醒勾选与自定义分钟;`EventWrite.url` 同步新增。
- 2026-09-30:前端 `static/js/event_form.js::collectReminders` 负责把勾选项与自定义输入框
  合成 `reminders` 数组(去重、从大到小),即本模块 `normalize_reminders` 的界面侧对应实现。
