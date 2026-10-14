# `app/settings_store.py`

非敏感配置覆盖项的读写。环境变量给出默认值,数据库 `app_settings` 允许在界面覆盖少量可调项;
SMTP 口令一类敏感值永远不进这里的任何键。

本轮(2026-10-14)新增「对外地址」覆盖项:`public_base_url` 在库里**只存 origin**
(`https://主机[:端口]`),完整地址 = 库里 origin + **环境变量给出的挂载前缀**。
理由:路径前缀是启动时挂载路由用的(`app/main.py`),运行时改它会让链接与路由错位。

## 常量与键

### `OVERRIDABLE_KEYS`

- **位置**:`app/settings_store.py:25`(`public_base_url` 一项在 `app/settings_store.py:31`)
- **用途**:允许在界面覆盖的键 → 对应 `Settings` 字段的白名单;
  `load_overrides` 与 `validate_overrides` 都只认这里的键
- **值**:`calendar_name`、`default_timezone`、`default_event_duration_minutes`、
  `mail_max_attempts`、`mail_backoff_minutes`、**`public_base_url`**
- **不变量**:不在白名单里的键既不读也不写 —— 敏感值没有「顺手加一个键」的入口

### `PUBLIC_BASE_URL_KEY`

- **位置**:`app/settings_store.py:37`
- **值**:`"public_base_url"`
- **用途**:需要按名字特判这个键的地方都引用它(它也是 `OVERRIDABLE_KEYS` 里的键名)
- **不变量**:**库里存的是 origin,不是完整地址**;页面、API、路由都不许把带路径的值写进这一行

### `INT_KEYS`

- **位置**:`app/settings_store.py:39`
- **值**:`{"default_event_duration_minutes", "mail_max_attempts"}`
- **用途**:需要按整数读写的键;`effective_settings` 靠它决定是否 `int(raw)`

## 函数

### `load_overrides(session) -> dict[str, str]`

- **位置**:`app/settings_store.py:48`
- **用途**:读出库里全部可覆盖项(键 → 字符串值)
- **返回**:只含 `OVERRIDABLE_KEYS` 里且值非 `None` 的行;没有覆盖时是空字典
- **不变量**:`public_base_url` 这一项的值是 **origin**(不是完整地址),调用方要用
  `compose_public_base_url` 拼前缀

### `normalize_public_base_origin(raw, settings) -> str`

- **位置**:`app/settings_store.py:57`
- **用途**:把「对外地址」输入规范成 origin;界面/API 都只接受 origin,不接受路径
- **接受**:`主机`、`主机:端口`、完整 URL(三种写法等价,没有 `://` 时按 `https://` 补)
- **路径规则**:省略路径、或只写到 `/` 时**按挂载前缀补齐**(直接接受);
  只要写了路径,就必须与 `urls.public_base_path(settings)` **逐字一致**,
  否则抛 `SettingValidationError`,文案说明「路径前缀只能改环境变量 `PUBLIC_BASE_URL` 并重建容器」
- **拒绝**:非 `http(s)`、没有主机名、带用户名或口令、带 query 或 fragment、
  端口不是数字(`urlsplit` 取 `.port` 时的 `ValueError` → 「端口不是一个数字」);
  空串也抛「对外地址不能为空」(撤销覆盖的空值语义由 `validate_overrides` 负责,不走这里)
- **返回**:`<scheme>://<主机>[:端口]`,**不带路径、不带尾斜杠**;主机名小写,
  IPv6 加方括号
- **参数**:`settings` 只用来取 `urls.public_base_path(settings)`(挂载前缀)
- **副作用**:无
- **不变量**:这个函数**绝不**决定路径 —— 返回值里永远没有路径;它也不接收
  用户名口令/query/fragment,避免把一个带凭据的地址写进库再渲染给所有人
- **测试**:`tests/test_settings_public_base_url.py::test_accepts_origin_only_and_strips_the_matching_path`、
  `::test_rejects_anything_that_is_not_a_usable_origin`(参数化 7 种非法值)、
  `::test_path_rule_error_explains_why`(文案含 `/calendar` 与「环境变量」)

### `compose_public_base_url(origin, settings) -> str | None`

- **位置**:`app/settings_store.py:121`
- **用途**:`origin + 当前挂载前缀` → 完整基地址;`effective_settings` 用它把库里的 origin
  变成 `Settings.public_base_url`
- **返回**:`f"{scheme}://{netloc}{urls.public_base_path(settings)}"`;
  以下情况返回 **`None`**(表示「这次覆盖值不可用」,调用方必须退回环境配置):
  origin 为空、scheme 不是 `http(s)`、没有主机名、带用户名口令、`urlsplit` 抛 `ValueError`
- **副作用**:无(纯函数)
- **不变量**:
  - **路径永远来自环境变量** —— 改 `.env` 并重建容器后,即使库里的 origin 覆盖还是旧的,
    拼出来的地址也会跟着新前缀走(见 `::test_environment_path_wins_even_with_a_stale_override`);
  - 库里被写脏时宁可**退回环境配置**,也不能渲染半截地址
    (见 `::test_garbage_override_falls_back_instead_of_breaking_links`)
- **注意**:它用 `parts.netloc` 原样拼接,因此**不做** `normalize_public_base_origin` 那套
  规范化(主机名不改大小写、端口合法性不再校验);它只负责「看一眼像不像 origin」
- **测试**:`tests/test_settings_public_base_url.py::test_override_changes_only_the_origin`、
  `::test_environment_path_wins_even_with_a_stale_override`、
  `::test_garbage_override_falls_back_instead_of_breaking_links`

### `validate_overrides(values, settings=None) -> dict[str, str]`

- **位置**:`app/settings_store.py:140`(`public_base_url` 分支在 `app/settings_store.py:151`)
- **本次变更**:签名新增可选的 `settings: Settings | None = None`(取挂载前缀用,
  不传时退回 `get_settings()`);新增 `public_base_url` 分支
- **空值语义**:`public_base_url` 为空白时**清洗成 `""`**(表示撤销覆盖、回到环境变量),
  这不是错误也不是删除 —— 真正的删除写在 `save_overrides` 里
- **行为**:
  - 只处理 `OVERRIDABLE_KEYS` 里、且值**不是 `None`** 的键(没提交的键不会被动撤销);
  - `public_base_url` → 走 `normalize_public_base_origin`,失败把文案收进 `errors[key]`;
  - `default_timezone` → 走 `app.timezones.resolve_timezone`,输入 `1`、`"1"`、`"北京时间"`、
    `"Asia/Shanghai"`、`"1. 北京时间(Asia/Shanghai)"` 都写入解析后的 IANA 名,
    识别不了时用 `timezone_help()` 的文案;
  - 其余键沿用原规则(日历名非空且 ≤100 字符;两个整数键各自区间;退避间隔为逗号分隔分钟数)
- **返回**:清洗后的键值字典(值都是字符串)**或**抛 `SettingValidationError`,
  错误按字段聚合在 `exc.fields` 里(路由/API 据此返回 422 字段错误)
- **副作用**:无(写库由 `save_overrides` 负责)
- **不变量**:`public_base_url` 清洗后的值**只能是 origin**;任何带路径的输入要么与
  挂载前缀一致(被剥掉路径),要么报错 —— 这里不得产生「运行时改挂载点」的副作用
- **测试**:`tests/test_settings_public_base_url.py::test_accepts_origin_only_and_strips_the_matching_path`、
  `::test_empty_value_means_revert_to_environment`、`::test_path_rule_error_explains_why`、
  `tests/test_timezones.py::test_settings_api_accepts_timezone_index`、
  `tests/test_api_settings.py::test_settings_update_and_validation`(非法值仍 422)、
  `::test_settings_form_partial_updates_and_validates`(表单直接提交 IANA 名仍可用)

### `save_overrides(session, values, settings=None) -> dict[str, str]`

- **位置**:`app/settings_store.py:206`
- **本次变更**:签名新增可选的 `settings: Settings | None = None`,透传给
  `validate_overrides`;新增空值语义
- **空值语义**:清洗后为 `""` 的键 = **删除该行**(撤销覆盖,回到环境变量);
  行不存在时什么都不做
- **行为**:先 `validate_overrides(values, settings)`,再逐键写入:不存在则 `insert`,
  存在则更新 `value`;最后 `session.flush()`(**不 commit**,提交由调用方决定)
- **返回**:清洗后的键值字典(与 `validate_overrides` 一致)
- **副作用**:写 `app_settings` 表 + `flush`;校验失败时抛 `SettingValidationError` 且不写任何行
- **不变量**:撤销覆盖必须是**删行**而不是写空串 —— 写空串会在库里留下一行
  「有覆盖但值为空」的残留(`load_overrides` 会把 `""` 当成一个覆盖值读出来),
  让「撤销」与「覆盖」混在一起
- **测试**:`tests/test_settings_public_base_url.py::test_clearing_the_override_falls_back_to_the_environment`、
  `::test_override_changes_only_the_origin`、
  `::test_saving_the_card_field_updates_the_effective_url`(页面路径)、
  `tests/test_api_settings.py::test_settings_update_and_validation`(API 路径)

### `effective_settings(session, base=None) -> Settings`

- **位置**:`app/settings_store.py:225`(`public_base_url` 分支在 `app/settings_store.py:239`)
- **用途**:把数据库覆盖项叠加到环境配置上,返回一份新的 `Settings`(**不落库、不改全局单例**)
- **本次变更**:`public_base_url` 走特殊处理 —— 库里的 origin 先经
  `compose_public_base_url(raw, resolved)` 与环境前缀拼成完整地址,才写进
  `Settings.public_base_url`;拼不出来(空/非法)时**不放进 `updates`**,直接沿用
  环境配置里的完整地址
- **参数**:`base` 为环境基线(不传时 `get_settings()`)
- **返回**:`resolved.model_copy(update=updates)`;没有任何可用覆盖时直接返回 `resolved`
- **行为细节**:数据库读不出来时记一条 debug 并退回环境配置;整数键解析失败跳过;
  `default_timezone` 最终仍用 `is_valid_timezone` 兜底,历史脏值会被丢弃
- **副作用**:无(只读库)
- **不变量**:
  - **库里只存 origin、路径永远来自环境变量** —— 这条函数是唯一的拼接点,
    别的地方不得自己拼 `origin + path`;
  - 覆盖值不可用时必须整体退回环境配置,不允许出现「半截地址」
- **测试**:`tests/test_settings_public_base_url.py::test_override_changes_only_the_origin`
  (断言前缀仍是 `/calendar`)、`::test_clearing_the_override_falls_back_to_the_environment`、
  `::test_garbage_override_falls_back_instead_of_breaking_links`、
  `tests/test_family_accounts.py::test_effective_settings_unchanged_by_accounts`

### `SettingValidationError`

- **位置**:`app/settings_store.py:42`
- **用途**:覆盖项校验失败时携带**按字段聚合**的中文错误
- **属性**:`fields: dict[str, str]`(键 → 文案);异常消息是所有文案的分号拼接
- **不变量**:错误键只能是 `OVERRIDABLE_KEYS` 里的键(路由直接把它当 422 的字段错误返回)

## 相关

- 设置页下拉框的「当前值不在常用清单中」分支依赖 `app.routers.pages._timezone_context`
  传入的 `timezone_known`(见 [`app-routers-pages.md`](app-routers-pages.md))。
- 「对外地址」卡片的上下文(`public_base_url_effective` / `..._env` / `..._override` /
  `..._input` / `..._suggested` / `public_base_path` / `host_port` / `serve_command`)由
  `app/routers/pages.py::_settings_form_context` 统一给出;宿主端口探测见
  [`app-host-port.md`](app-host-port.md)。

## 变更记录

- 2026-09-29:`default_timezone` 支持序号与中文名。
- 2026-10-14:新增对外地址覆盖项 —— `PUBLIC_BASE_URL_KEY`、
  `normalize_public_base_origin`(只收 origin;写了路径必须与挂载前缀逐字一致)、
  `compose_public_base_url`(origin + 环境前缀,拼不出来回退 `None`);
  `validate_overrides` / `save_overrides` 增加可选 `settings` 参数并把空值语义定为
  「撤销覆盖」(清洗成 `""` → 删行);`effective_settings` 改为先拼接再写入
  `Settings.public_base_url`。不变量:**库里只存 origin,路径永远来自环境变量**。
  测试见 `tests/test_settings_public_base_url.py`(25 个用例)。