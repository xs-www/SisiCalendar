# `app/timezones.py`

给用户看的常用时区清单,以及「序号 / 中文名 / IANA 名 → IANA 名」的统一解析。

设计动机:iOS 快捷指令无法取得设备的 IANA 时区名,只能人工填写。为了不让人去记
`Australia/Brisbane`,`/admin/settings` 给出一个带**固定序号**的下拉框,接口则接受这个序号。

## 数据结构

### `TimezoneChoice`

- **位置**:`app/timezones.py:26`
- **字段**:`index`(对外 1 基序号)、`iana`(规范时区名)、`label`(中文名,如「北京时间」)、
  `region`(分组名)、`aliases`(别名,如「北京」「中国」)
- **不变量**:`index` 是**对外契约**,一经发布不得调整顺序或改号

### `TIMEZONE_CHOICES: tuple[TimezoneChoice, ...]`

- **位置**:`app/timezones.py:39`
- **内容**:30 项,`1 = 北京时间(Asia/Shanghai)`、`11 = 悉尼时间(Australia/Sydney)`、
  `12 = 布里斯班时间(Australia/Brisbane)`、`30 = 协调世界时(UTC)`
- **不变量**:序号从 1 连续编号且与元组下标一致(`index == 位置 + 1`);新增只能追加到表尾
- **测试**:`test_indexes_are_contiguous_and_unique`、`test_beijing_and_sydney_are_in_the_list`

### `REGION_ORDER` / `MAX_INDEX`

- **位置**:`app/timezones.py:36`、`app/timezones.py:74`
- **用途**:分组渲染顺序;序号上界(错误提示里要写「1-30」)

## 函数

### `resolve_timezone(value) -> str | None`

- **位置**:`app/timezones.py:144`
- **用途**:把外部传入的时区写法解析成入库用的 IANA 名
- **接受的写法**(大小写不敏感):
  - 整数或数字字符串:`1`、`"1"`、`"#1"`、`"序号1"`、`"第 11 项"`、`"11号"`;
    `0` 视为第 1 项(兼容从 0 开始数的调用方)
  - 中文名或别名:`"北京时间"`、`"北京"`、`"悉尼时间"`、`"悉尼"`
  - 下拉框整行文本:`"11. 悉尼时间(Australia/Sydney)"`、`"3. 布里斯班时间"`
  - IANA 名:`"Asia/Shanghai"`、`"europe/london"`(返回规范写法)
- **返回**:IANA 名;无法识别、空值、`bool`、非整数浮点、越界序号一律返回 `None`
- **拒绝**:偏移量与缩写(`+10:00`、`GMT+10`、`AEST`)——库里必须存可还原的 IANA 名
- **副作用**:无(内部缓存 `available_timezones()`)
- **测试**:`tests/test_timezones.py::test_resolve_timezone`、`::test_resolve_timezone_rejects_unknown`

### `canonical_iana(text) -> str | None`

- **位置**:`app/timezones.py:103`
- **用途**:把可能是 IANA 名的文本规范成标准写法(大小写不敏感)
- **返回**:规范名;不是合法时区名时 `None`
- **实现要点**:用 `available_timezones()` 建小写索引,而不是直接 `ZoneInfo(name)`——
  后者在 Windows 上会接受 `europe/london`,在 Linux 上不会,行为不可预测
- **测试**:`test_resolve_timezone[europe/london-Europe/London]`

### `choice_at(index) -> TimezoneChoice | None`

- **位置**:`app/timezones.py:114`
- **用途**:按 1 基序号取一项;`0` 返回第一项;越界返回 `None`

### `timezone_help(value=None) -> str`

- **位置**:`app/timezones.py:223`
- **用途**:校验失败时的中文提示,必须告知「还能填序号」以及序号区间
- **示例**:`未识别的时区:Mars/Olympus。可填 IANA 名(如 Asia/Shanghai),也可填设置页列出的序号 1-30(1 = 北京时间)或中文名。`
- **测试**:`test_invalid_timezone_error_mentions_index`

### `describe_timezone(value) -> str | None` / `timezone_label(iana) -> str | None`

- **位置**:`app/timezones.py:212`、`app/timezones.py:202`
- **用途**:给界面/日志用的展示文本,如 `1 · 北京时间 (Asia/Shanghai)`

### `timezone_choices() -> list[dict]`

- **位置**:`app/timezones.py:233`
- **返回**:`[{"index", "iana", "label", "region"}, ...]`,供 `GET /api/v1/settings` 返回
- **测试**:`test_settings_api_exposes_timezone_choices`

### `timezone_groups() -> list[tuple[str, list[TimezoneChoice]]]`

- **位置**:`app/timezones.py:246`
- **用途**:按 `REGION_ORDER` 分组,供模板渲染 `<optgroup>`
- **测试**:`test_groups_cover_every_choice`

### `known_iana_names() -> frozenset[str]` / `iana_names(choices=TIMEZONE_CHOICES) -> tuple[str, ...]`

- **位置**:`app/timezones.py:256`、`app/timezones.py:261`
- **用途**:判断「当前值是否已在选项中」(`templates/partials/settings_form.html`);生成
  `app/routers/pages.py::COMMON_TIMEZONES`(缺少 tzdata 时的回退清单)

## 变更记录

- 2026-09-29:新建。清单 30 项、序号固定;解析支持序号/中文名/IANA 名,拒绝偏移量。
