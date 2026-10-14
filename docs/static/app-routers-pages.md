# `app/routers/pages.py`

管理页面与 HTMX 局部片段。

家庭版(2026-10)起,页面按角色分成三层:所有成员都能进的日历/表单/「我的设置」,
只有管理员能进的成员/设置/同步;所有写操作都走 HTMX 局部片段或 JSON API,
两条路径都要求登录 + CSRF。本文件同时给出 `_base_context` 这条页面基线上文,
成员页面与管理员页面共用它。

本轮(2026-10-12)另加两件事:**公开前缀感知** —— 所有页面链接由模板的 `bp()`
渲染、所有跳转由 `app/urls.py::url()` 拼;**订阅面板的固定地址** —— 账号那条由服务端
密钥派生、永不变更的订阅地址可以直接展示,但界面不给停用/撤销。

本轮(2026-10-14)新增设置页的「对外地址(Tailscale)」卡片与宿主端口探测:
`_tailnet_origin_hint`(只从 tailnet 域名自动填入候选)、`_serve_command`(按
`HOST_PORT` 与挂载前缀生成命令)、`_settings_form_context`(整页与 HTMX 片段共用的上下文)、
`POST /admin/actions/host-port-check`(管理员点击才跑的只读探测)。
**界面里只能改 origin**,路径前缀仍由环境变量决定。

## 新增(2026-10-12:公开前缀根路由)

### `FEED_PLACEHOLDER_TEXT`

- **位置**:`app/routers/pages.py:245`
- **内容**:一段中文纯文本,以「此地址不再提供日历。请使用属于你自己的订阅地址……」开头,
  并给出地址形状 `/calendar/sisi_feed_xxxx.ics`
- **不变量**:它是**唯一**给订阅端看的"旧地址已取消"文案;文案里的路径形状要与
  `app/urls.py::FEED_SUFFIX` 一致

### `public_root(request) -> Response`

- **位置**:`app/routers/pages.py:251`;由 `app/main.py` 用
  `app.add_api_route(base_path, pages.public_root, methods=["GET", "HEAD"],
  include_in_schema=False)` 挂在**前缀本身**(`/calendar`)上
- **用途**:无令牌的旧订阅地址彻底取消,但要让存错地址的订阅端拿到"看得懂的一句人话",
  而不是被 303 带到一个 HTML 页面(那会被 iPhone 存成一个 HTML「日历」)
- **行为**:
  - `Accept` 含 `text/html`(浏览器手动输错地址)→ **303 → `urls.url("/admin")`**
  - 否则(日历客户端一般发 `*/*` 或 `text/calendar`)→ **404**、`text/plain; charset=utf-8`、
    `Cache-Control: no-store`,正文为 `FEED_PLACEHOLDER_TEXT`
- **副作用**:无
- **不变量**:这里**绝不**可以返回任何 ICS 内容或重定向到订阅文件;
  它也**不是**「首位管理员的可见范围」那种兼容别名(见 `AGENTS.md` 不变量 14)
- **测试**:`tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`、
  `tests/test_family_web.py::test_legacy_tokenless_address_is_gone`、
  `tests/test_feed.py::test_legacy_tokenless_paths_are_gone`(断言 `/calendar` 与
  `/calendar/` 的正文含「不再提供日历」)

### `GET /` — `root`

- **位置**:`app/routers/pages.py:270`(函数 `:271`),`include_in_schema=False`
- **用途**:一条路由同时兜住裸根路径与前缀根路径(带尾斜杠的那种)
  - 当次请求前缀非空**且** `request.url.path.rstrip("/") == prefix` → 交给 `public_root(request)`
    (即 `/calendar/` 与 `/calendar` 行为一致)
  - 否则 → **303 → `urls.url("/admin")`**(本机 `http://localhost:443/` 跳管理页)
- **不变量**:判断必须用 `urls.current_prefix()`(当次请求绑定值),不要写死 `/calendar`;
  **不带**尾斜杠的 `/calendar` 走的是 `app/main.py` 里那条 `add_api_route`,两条路径行为必须一致
- **测试**:`tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`
  (覆盖 `/calendar/` 的浏览器 303 与机器 404)、
  `::test_base_url_without_path_falls_back_to_bare_paths`

## 新增

### `_timezone_context(current=None) -> dict[str, object]`

- **位置**:`app/routers/pages.py:119`
- **用途**:一次性给出时区相关的模板变量,避免各页面各拼一份
- **返回的键**:
  - `timezones`:`all_timezones()` 的全部 IANA 名(新建/编辑页候选)
  - `common_timezones`:`COMMON_TIMEZONES`(带序号的常用清单,缺少 tzdata 时的回退来源)
  - `timezone_choices`:`TIMEZONE_CHOICES`(渲染带序号的候选)
  - `timezone_groups`:`[(地区, [选项...])]`,渲染 `<optgroup>`
  - `timezone_known`:清单内 IANA 名的集合,用于判断是否要补「当前值」选项
  - `current_timezone`:`current` 去空白后的字符串
- **参数**:`current` 为当前生效的时区,用于标记 `<option selected>`
- **副作用**:无
- **测试**:`tests/test_timezones.py::test_settings_page_renders_timezone_dropdown`、
  `::test_settings_page_keeps_custom_timezone`

## 变更

### `COMMON_TIMEZONES`

- **位置**:`app/routers/pages.py:90`
- **本次变更**:由手写 16 项改为 `list(iana_names())`(来自 `app/timezones.py`,30 项)
- **不变式**:`all_timezones()` 在缺少 tzdata 时回退到它,内容必须都是合法 IANA 名

### 页面上下文(默认时区 / 时区候选)

- **位置**:`event_new_page`(`:357`)、`event_edit_page`(`:406`)、`settings_page`(`:1243`)、
  `save_settings_action`(`:1867`)、`settings_form_partial`(`:2169`)
- **本次变更**:原来的 `"timezones": all_timezones()` 改为展开 `**_timezone_context(...)`;
  设置页与设置片段把当前值传成 `overrides.get("default_timezone", settings.default_timezone)`
- **说明**:保存失败时(`save_settings_action` 的 422 分支)下拉框回到**仍然生效**的时区,
  用户填错的原始值只出现在错误提示里
- **2026-10-08**:「我的设置」新增个人时区下拉(`partials/me.html`),候选同样来自
  `_timezone_context(current.user.timezone or "")`;空值表示「跟随应用默认时区」

### `_sync_partial(...)`

- **位置**:`app/routers/pages.py:2130`
- **本次变更**:上下文新增 `default_timezone = settings.default_timezone`,
  邮件队列表格用它把 `created_at` / `sent_at` / `next_attempt_at` 渲染成本地时间
- **不变式**:`sync_page` 走 `_base_context`,同样提供 `default_timezone`,两条渲染路径行为一致

### `_deliveries(session, limit=30)`

- **位置**:`app/routers/pages.py:190`
- **本次变更**:无(仍按 `created_at` 倒序取最近 30 条);新增列直接使用已有的
  `created_at` / `sent_at`,**不需要数据库迁移**

## 新增:远程接入令牌管理片段(HTMX)

路由位于 `app/routers/pages.py:1477` 起,渲染 `templates/partials/tokens.html`,
目标元素 `#agent-tokens`,与「同步人员」片段(`#recipients`)同构。

### `_token_partial(request, session, settings, *, notice, error, status_code, new_token) -> Response`

- **位置**:`app/routers/pages.py:1477`
- **用途**:统一渲染令牌片段
- **上下文**:`tokens`(`token_store.list_tokens`)、`users`、`shared_calendars`、`csrf_token`、
  `default_timezone`、`new_token`(仅创建成功那一次非空)、`notice`、`error`
- **不变量**:`new_token` 只允许出现在**创建成功的响应**里;任何后续渲染都必须为 `None`,
  否则明文会在页面里反复出现

### `GET /admin/partials/tokens`

- **位置**:`app/routers/pages.py:1506`(函数 `:1507`);依赖 `AdminDep`(需登录 + 管理员)
- **用途**:列出全部令牌(含已停用)

### `POST /admin/partials/tokens`

- **位置**:`app/routers/pages.py:1516`(函数 `:1517`);依赖 `CsrfAdminDep`(需登录 + 管理员 + CSRF)
- **表单**:`label`、`user_id`(归属账号)、`calendar_id`(默认日历,**留空 = 该账号的个人日历**)、
  `acknowledge`
- **行为**:未勾选 `acknowledge` → 422;`label` 空或超长 → 422;归属账号缺省落到首位管理员
  (没有任何账号时 409);指定了 `calendar_id` 但该账号对它没有写权限 → 422,不写入
- **副作用**:写库(只写哈希)、审计 `agent_token_add`(detail 为令牌 id,**不含明文**)
- **测试**:`tests/test_agent_tokens.py::test_plaintext_is_shown_once_and_only_hash_is_stored`、
  `::test_creation_requires_acknowledgement_and_label`、`::test_plaintext_never_reaches_logs`;
  归属与默认日历的渲染:`tests/test_family_pages.py::test_token_partial_lists_owner_and_shared_calendars`

### `POST /admin/partials/tokens/{token_id}` / `.../toggle` / `.../delete`

- **位置**:`app/routers/pages.py:1583`(函数 `rename_token_route` `:1586`)、
  `:1615`(`toggle_token` `:1620`)、`:1639`(`revoke_token` `:1644`)
- **用途**:改名、启用/停用、撤销(硬删除)
- **行为**:id 不存在 → 404;改名校验失败 → 422 且不写入
- **副作用**:写库 + 审计 `agent_token_rename` / `agent_token_toggle` / `agent_token_revoke`
- **说明**:撤销后该令牌**下一次请求即失效**;若因此一枚不剩,`/api/agent` 回到 404
- **测试**:`tests/test_agent_tokens.py::test_token_can_be_renamed`、
  `::test_disabled_token_is_rejected_and_can_be_reenabled`、
  `::test_revoked_token_stops_working_immediately`、`::test_unknown_token_id_returns_404`

### `settings_page` 上下文

- **位置**:`app/routers/pages.py:1243`
- **本次变更(2026-10-08)**:新增 `"tokens": token_store.list_tokens(session)`,供设置页直接渲染令牌卡片
- **本次变更(2026-10-14)**:不再自己拼「对外地址」那几个键,改为在上下文里展开
  `**_settings_form_context(request, settings, overrides)`(`app/routers/pages.py:1258`)——
  与 HTMX 只换片段那条路径共用同一套变量(见下文「对外地址(Tailscale)卡片」)

## `__all__`

- **位置**:`app/routers/pages.py:2180`
- **本次变更**:补充导出 `timezone_groups`,方便测试与外部脚本引用清单;
  另导出 `MEMBER_ROLE_LABELS`(共享日历的三档成员权限中文名)

## 新增:日程表单的提醒上下文(2026-09-30)

### `event_new_page` / `event_edit_page`

- **位置**:`app/routers/pages.py:357` / `app/routers/pages.py:406`
- **本次变更**:上下文新增两个键
  - `reminder_options`:`app/reminders.py::preset_options(...)`,新建页传空列表,编辑页传
    `serialize_event(event)["reminders"]`
  - `custom_reminders`:`app/reminders.py::custom_reminder_text(...)`,只回填**非预设**的提醒
- **说明**:新建页的 `event` 字典也补了 `url: ""` 与 `reminders: []`,模板不再需要兜底判断
- **不变量**:自定义值不会同时出现在勾选项与输入框里;勾选项顺序由 `PRESET_REMINDERS` 决定
- **测试**:`tests/test_reminders.py::test_event_form_renders_reminder_controls`、
  `::test_edit_form_marks_existing_reminders`

## 新增:家庭版页面与片段(2026-10-08)

### 基线上文 `_base_context(request, session, settings, *, active) -> dict`

- **位置**:`app/routers/pages.py:198`
- **用途**:所有页面共用的上下文:当前账号、角色、CSRF、生效设置与展示用时间
- **关键键**:
  - `current_user`:`optional_user(request, session)` 的结果;**未登录时为 `None`**,模板必须判空
  - `is_admin`:登录账号的角色是否为 `admin`(导航栏用它决定是否渲染管理入口)
  - `csrf_token`:未登录时是空串,因此基模板里的退出表单只对已登录用户有意义
  - `default_timezone`:该账号的**个人时区**,缺失时回落到 `settings.default_timezone`
    (`app/accounts.py::user_timezone`)
  - `system_timezone`:应用默认时区,与个人时区分开传,便于界面提示两者差异
  - `now_local`:按个人时区渲染的当前时间(`generated_at_display`)
  - `public_base_url`(`app/routers/pages.py:213`,2026-10-12 起):`urls.public_base_url(settings)`,
    供只读展示"公开基地址"的页面片段使用
- **不变量**:这是**唯一**注入 `current_user` / `is_admin` / `default_timezone` 的地方;
  新页面必须经它取上下文,不要自己再读一遍 request。
  它**不含**页面链接的前缀 —— 链接前缀由模板里的 `bp()` 按当次请求渲染
  (见 `docs/static/app-main.md`、`docs/static/app-deps.md`)

### `_csrf_of(request, session) -> str`

- **位置**:`app/routers/pages.py:219`
- **用途**:给局部片段取当前账号的 CSRF 令牌
- **返回**:已登录账号的令牌;**未登录返回空串**(不抛异常)
- **说明**:片段渲染函数(`_members_partial`、`_recipient_partial`、`_sync_partial`、
  `_family_feed_partial`)与 `_token_partial` 都用它;`_me_partial` / `_feed_partial`
  走依赖注入的 `current.csrf_token`,两条路径等价

### `_calendar_context(session, user) -> dict`

- **位置**:`app/routers/pages.py:224`
- **用途**:日历页与事件表单共用的日历清单(带颜色与可写标记)
- **返回**:
  - `access`:`app/accounts.py::CalendarAccess`,前后端判定可见/可写的唯一依据
  - `calendar_options`:`[{id, name, kind, color, role, can_write}]`,**只含本人可见的日历**
  - `default_calendar_id`:`access.default_write_calendar_id()`,优先个人日历,没有可写日历时为 `None`
  - `can_write_anything`:bool,决定「新建日程」按钮是链接还是禁用按钮
- **不变量**:清单**绝不**包含别人私人日历;`can_write` 必须来自 `access`,不要在模板里自己推断

### `GET /admin`(`calendar_page`)

- **位置**:`app/routers/pages.py:279`(函数 `:280`);依赖 `UserDep`(登录即可,不限管理员)
- **用途**:日历主页(本人可见日历的合并视图)
- **角色分级**:管理员拿到完整的 `overview(...)`(版本、体积、邮件队列统计);
  成员只拿到 `dirty` / `version` / `size_bytes` / `generated_at` / `last_error` 五个键,
  **邮件队列与收件人统计属于管理员视图**
- **测试**:`tests/test_family_pages.py::test_admin_pages_render`、
  `::test_member_pages_render`、`::test_member_nav_hides_admin_links`、
  `::test_calendar_page_exposes_calendars_for_the_sidebar`、
  `tests/test_family_web.py::test_member_can_open_calendar_and_profile`

### `GET /admin/events/new`(`event_new_page`)

- **位置**:`app/routers/pages.py:357`;依赖 `UserDep`
- **用途**:新建日程表单
- **日历下拉**:`?calendar=<id>` 只在**该账号可写**时才被选中,否则回落到
  `default_calendar_id`;两者都为空时 `event_writable=False`,模板把保存按钮置灰
- **上下文**:`selected_calendar_id`、`event_writable`、`calendar_options`、`default_calendar_id`、
  `can_write_anything`
- **测试**:`tests/test_family_pages.py::test_member_pages_render`

### `GET /admin/events/{event_id}`(`event_edit_page`)

- **位置**:`app/routers/pages.py:406`;依赖 `UserDep`
- **越界处理**:`get_event(..., calendar_ids=access.viewable_ids())` 返回 `None` 时
  **404**(「事件不存在、已删除,或不属于你可访问的日历」)——不在可见范围内的私人日程
  既不显示也不泄露是否存在
- **只读态**:`event_writable = access.can_write(event.calendar_id)`;为假时模板隐藏删除按钮、
  置灰保存,并显示「你没有修改该事件的权限」
- **测试**:`tests/test_family_web.py::test_member_cannot_open_someone_elses_event_page`、
  `tests/test_family_pages.py::test_event_form_for_shared_calendar_is_read_only_for_member`

### `_me_context(session, settings, current) -> dict[str, object]`

- **位置**:`app/routers/pages.py:442`
- **用途**:「我的设置」**整页与局部片段共用**的上下文
- **为什么必须共用**:整页 `me.html` 就是 `{% include "partials/me.html" %}`,
  两边喂的变量只要差一个,Jinja 默认的 `Undefined` 只会**安静地渲染成空串** ——
  页面照样 200,但「我的订阅地址」是空的、邮箱验证状态也看不见
  (源码注释点名的抓法:`tests/test_family_pages.py::test_all_pages_render_with_strict_undefined`
  把 `templates.env.undefined` 换成 `StrictUndefined`,缺变量当场炸)
- **返回的键**:
  - `profile`(display_name/email/color/timezone)
  - `feed_tokens`:该账号自己的令牌(`user_id=current.user_id`,含那条固定地址)
  - `fixed_feed_url`:该账号**固定**订阅地址的绝对地址
    (`app/feed_tokens.py::account_subscription_url`)
  - `fixed_token_id`:固定地址那一行的 id(`fixed_account_token` 的 `id`,取不到为 `None`);
    模板据此把该行标「固定」并隐藏停用/撤销按钮
  - `verify_state`:`{needs_verification, verified_at, email}`,由
    `app/email_verify.py::needs_email_verification` 判定;模板据此渲染待验证警告块或
    「邮箱已验证(时间)」提示
  - `new_token`:**固定为 `None`** —— 它就是 `partials/feeds.html` 要的临时明文键名,
    默认「这次没有新明文」,只有 `_me_partial` 被显式传入时才会覆盖
  - `subscription_prefix`:`urls.public_base_url(settings)`
  - `**_timezone_context(current.user.timezone or "")`(时区候选 + 当前值)
- **不变量**:这是「我的设置」**唯一**的上下文来源;
  `_me_partial` 只额外加 `csrf_token` / `current_user` / `default_timezone` / `notice` /
  `error` / `new_token`,**不得**再各自拼一套「我的设置」变量

### `GET /admin/me`(`me_page`)/ `GET /admin/partials/me`(`me_partial`)

- **位置**:`app/routers/pages.py:510`(函数 `:511`)/ `:528`(函数 `:529`);依赖 `UserDep`
- **用途**:个人资料、改口令、我自己的订阅地址(所有成员可用,不需要管理员)
- **上下文**:
  - `me_page`:`_base_context(...)`(基线上文,**不含**时区候选)
    + `_me_context(...)` + `notice` / `error`(取自查询参数),渲染 `me.html`;
  - `me_partial`:`_me_partial(...)`(`:479`),内部同样是 `_me_context(...)` 再加
    `csrf_token` / `current_user` / `default_timezone = user_timezone(...)` / `notice` /
    `error` / `new_token`,渲染 `partials/me.html`(目标 `#me-panel`)
  - 整页里 `partials/me.html` 还会自己补 `timezone_groups` / `timezone_known` /
    `current_timezone` / `default_timezone` 的默认值(`templates/me.html`),
    但**真正有值的候选来自 `_me_context` 的 `_timezone_context`**,两者一致
- **不变量**:成员在这里**只能**看到并操作自己的订阅令牌;管理员在 `/admin/me` 也**没有**
  操作别人令牌的入口;整页与片段必须给出同一份「我的设置」内容
- **测试**:`tests/test_family_pages.py::test_member_partials_render`、
  `::test_all_pages_render_with_strict_undefined`(整页 + 片段 + 管理面全部页面在
  `StrictUndefined` 下渲染,防的就是上下文缺键)、
  `tests/test_family_web.py::test_member_can_open_calendar_and_profile`、
  `tests/test_family_pages.py::test_profile_update_starts_email_verification`(片段里出现「验证」提示)

### `POST /admin/actions/profile`(`save_profile_action`)

- **位置**:`app/routers/pages.py:538`(函数 `:539`);依赖 `CsrfDep`(登录 + CSRF);表单字段 `:543`-`:546`
- **表单**:`display_name`、`email`、`color`、`timezone`
- **行为**:
  1. 记下 `previous_email`(小写去空白),走 `app/accounts.py::update_profile` 并提交;
     校验失败(`AccountError`)→ 回滚 + **422** 并回显全部字段错误;
     审计 `profile_save/success`;
  2. **邮箱真的变了**(小写比较,且新值非空)才进入重新验证:
     `email_verify.mark_unverified`(作废旧码)→ `email_verify.issue_code` →
     `sync_user_recipient`(地址变了收件人跟着变)→ `commit()`;
     - 发信成功 → 审计 `verify_code_sent/success`,提示
       「邮箱已更新,验证码已发到 `mask_email(邮箱)`。」
     - 发信失败 → 提示「邮箱已更新,但验证码邮件发送失败,请在「邮箱验证」页重新发送。」
  3. 邮箱没变(只改显示名/颜色/时区)→ 提示「个人资料已保存,邮件收件范围已同步。」,
     **不动验证状态**
- **副作用**:写库、同步账号收件人、可能登记新验证码并**发一封验证码邮件**、审计
- **不变量**:
  - 邮箱唯一 —— 想占用别人的邮箱 → 422「已被其他账号使用」;
  - **比较必须在小写去空白之后做**:大小写不同的同一地址不算"改变",否则每次保存都会重发验证码;
  - 未验证期间**不建账号收件人**,也就不会把日程发到一个还没确认归属的地址
- **测试**:`tests/test_family_pages.py::test_profile_update_starts_email_verification`
  (保存成功、`email_verified_at is None`、已登记验证码、收件人表仍为空)、
  `::test_profile_rejects_invalid_timezone`、`::test_profile_cannot_steal_another_accounts_email`;
  改邮箱后重新验证的端到端链路:`tests/test_email_verify.py::test_verified_member_who_changes_email_must_verify_again`

### `POST /admin/actions/password`(`change_password_action`)

- **位置**:`app/routers/pages.py:589`(函数 `:590`);依赖 `CsrfDep`
- **表单**:`current_password`、`new_password`、`new_password_confirm`
- **行为**:当前口令不对 → 401 + 审计 `password_change`(denied);两次新口令不一致 → 422;
  新口令不满足策略 → 422
- **副作用**:写库(Argon2 哈希)、审计 `password_change`(success)
- **测试**:`tests/test_family_pages.py::test_password_change_requires_current_password`

### 我的订阅地址片段

- **位置**:`_feed_partial`(`:628`)、`GET/POST /admin/partials/feeds`(`:672` / `:689`)、
  `.../feeds/{token_id}/toggle`(`:727`,函数 `:732`)、`.../feeds/{token_id}/delete`
  (`:777`,函数 `:782`)
- **用途**:每个账号管理**自己的** ICS 订阅地址(渲染 `partials/feeds.html`,目标 `#feeds`)
- **本次变更(2026-10-12)**:上下文新增固定地址三件套
  - `fixed_feed_url`:`app/urls.py::subscription_url(settings, account_feed_token(settings, user_id))`,
    即 `<公开基地址>/<固定令牌>.ics`;`user_id` 为空(家庭令牌片段)时为 `None`
  - `fixed_token_id`:`feed_tokens.fixed_account_token(...)` 的 id,取不到为 `None`
  - `subscription_prefix`:`urls.public_base_url(settings)`(临时明文地址那一块拼绝对地址用)
- **归属校验**:toggle / delete 先取令牌,`row is None or row.user_id != current.user_id`
  → **404**(不是 403,避免泄露令牌是否存在)
- **固定地址保护**(`app/feed_tokens.py::is_fixed_account_token`):
  - toggle → **409**「这是账号的固定订阅地址,不能停用。需要临时地址请另建一条。」
  - delete → **409**「这是账号的固定订阅地址,不能撤销(它随时可以重新展示给本人)。」
  - 两种情况都不写库、不审计,只把片段按 409 渲染出来
- **副作用**:写库 + 审计 `feed_token_add` / `feed_token_toggle` / `feed_token_revoke`
- **不变量**:
  - `new_token` 只在创建成功的响应里非空(临时令牌);
  - **固定地址的明文不是"一次性"的** —— 它由服务端密钥派生,每次渲染都能重新算出来,
    所以页面**可以长期显示**它(这是与其它敏感值的唯一例外,见 `AGENTS.md` 不变量 23);
  - 列表只显示 `token_hash[:16]` 指纹,固定地址的明文只出现在 `fixed_feed_url` 输入框里
- **测试**:`tests/test_family_pages.py::test_my_feed_is_stable_and_temporary_token_shown_once`、
  `::test_fixed_address_cannot_be_disabled_or_revoked`、
  `::test_member_cannot_touch_another_users_feed_token`

### 成员与邀请码(管理员)

- **位置**:页面 `members_page`(`:890`,函数 `:891`)、片段 `members_partial`(`:908`,函数 `:909`)/
  `invites_partial`(`:918`,函数 `:919`);
  写操作 `create_invite_action`(`:928`,函数 `:929`)、`toggle_invite_action`(`:977`,函数 `:982`)、
  `delete_invite_action`(`:1001`,函数 `:1006`)、`toggle_member_action`(`:1025`,函数 `:1030`)、
  `reset_member_password_action`(`:1053`,函数 `:1058`)、`set_member_role_action`(`:1084`,函数 `:1089`)、
  `delete_member_action`(`:1124`,函数 `:1129`)、`set_calendar_role_action`(`:1156`,函数 `:1161`)、
  `create_shared_calendar_action`(`:1205`,函数 `:1208`)
- **依赖**:读走 `AdminDep`,写走 `CsrfAdminDep`(成员一律 403)
- **上下文**:`_members_context`(`:838`)提供 `members`、`invites`、`shared_calendars`
  (由 `_shared_calendar_entries`(`:830`)拼成「日历 + 该日历上各成员角色」)、
  `member_role_labels`(`owner`=可管理 / `editor`=可编辑 / `viewer`=只读)、`role_labels`(admin/member),
  以及 `invite_base_url`(`f"{urls.public_base_url(settings)}/register?code="`,
  `app/routers/pages.py:847`)与 `public_base_url`(`:848`);
  **四个一次性明文键也有默认值**(`new_invite` / `new_invite_url` / `new_password` /
  `reset_username` 全为 `None`,`:851`–`:854`)——整页 `/admin/members` 也经 include 渲染
  `partials/members.html`,缺键时 Jinja 只会渲染空串,所以默认值必须在这里给
- **行为**:
  - 邀请码:`valid_days` 非整数 → 422;`invites.create_invite` 校验失败(如 `max_uses` 越界)→ 422
  - **邀请链接用 `urls.invite_url(settings, plaintext)` 生成**(`app/routers/pages.py:972`),
    即 `<公开基地址>/register?code=<明文>`(明文经过 `quote`),与 `_members_context` 里
    给列表用的 `invite_base_url` 前缀**必须一致**
  - 停用/启用成员、改角色、重置口令、删除账号:目标不存在 → 404
  - **自锁保护**:停用或删除当前登录账号 → 409「不能停用/删除当前登录的管理员账号」;
    把当前登录账号降级成非管理员 → 409;删除后管理员会归零 → 409「至少需要保留一个管理员」
  - 共享日历权限:账号或共享日历不存在 → 404;`set_membership` 校验失败 → 422
- **副作用**:写库 + 审计 `invite_create` / `invite_toggle` / `invite_delete` /
  `member_toggle` / `member_reset_password` / `member_role` / `member_delete` /
  `calendar_role` / `shared_calendar_create`(邮箱类 detail 一律 `mask_email`)
- **测试**:`tests/test_family_pages.py::test_members_page_lists_all_accounts`、
  `::test_admin_can_disable_and_promote_member`、`::test_admin_cannot_disable_or_delete_self`、
  `::test_admin_deleting_member_keeps_last_admin`、
  `::test_admin_creates_shared_calendar_with_viewer_defaults`、
  `::test_member_calendar_role_change_requires_admin`、
  `tests/test_public_prefix.py::test_invite_link_contains_public_base_url`
  (创建响应与列表里都是 `https://example.test/calendar/register?code=…`);
  注册链路:`tests/test_family_web.py::test_register_page_and_submit`、`::test_register_consumes_invite_once`

### 家庭订阅地址片段(管理员)

- **位置**:`_family_feed_partial`(`:1666`)、`GET/POST /admin/partials/family-feeds`
  (`:1693` / `:1703`)、`.../family-feeds/{token_id}/toggle`(`:1733`,函数 `:1738`)、
  `.../family-feeds/{token_id}/delete`(`:1757`,函数 `:1762`)
- **用途**:管理 `user_id=None` 的**家庭令牌**(渲染 `partials/family_feeds.html`,目标 `#family-feeds`)
- **归属校验**:`row is None or row.user_id is not None` → 404,即这个片段**只管家庭令牌**,
  成员个人令牌必须回到各自 `/admin/me`
- **上下文**:`feed_tokens`(只含家庭令牌)、`new_token`、`csrf_token`、
  `subscription_prefix = urls.public_base_url(settings)`(`:1684`)、`default_timezone`、
  `notice`、`error` —— **没有** `fixed_feed_url` / `fixed_token_id`(家庭令牌没有固定地址一说)
- **副作用**:写库 + 审计 `family_feed_add` / `family_feed_toggle` / `family_feed_revoke`
- **测试**:`tests/test_family_pages.py::test_admin_partials_render`、
  `::test_member_is_locked_out_of_admin_surfaces`

### 邮件收件人片段(管理员)

- **位置**:`_recipient_partial`(`app/routers/pages.py:1313`)、`GET/POST /admin/partials/recipients`
  (`:1343` / `:1353`)、`.../recipients/{id}/toggle`(`:1406`,函数 `:1411`)、
  `.../recipients/{id}/delete`(`:1447`,函数 `:1452`)
- **本次变更**:
  - 上下文新增 `users`,模板据此渲染「范围」列(三种,见 `docs/static/templates.md`):
    `user_id` 非空 → 「本人可见日历」;`user_id` 为空但 `scope_user_id` 非空 →
    「沿用某成员的可见日历 + 历史绑定」;两者都为空 → 「家庭共享日历」
  - 新增外部邮箱必须勾 `acknowledge`,否则 422(提示「外部邮箱会收到家庭共享日历
    (不含任何人的私人日程)」);重新启用一个已停用收件人也要 `acknowledge`
  - 新行固定写入 `user_id=None`(外部收件人,`scope_user_id` 也为 `None`);
    账号收件人与 `scope_user_id` 只能由 `users.email` / 引导与迁移派生
- **本次变更(2026-10-11)**:「范围」列随 `scope_user_id` 落地扩为三种显示;
  片段**上下文未变**(仍是 `recipients`、`users`、`csrf_token`、`default_timezone`、
  `mail_configured`、`notice`、`error`),三种范围完全由 `recipients` 行的
  `user_id` / `scope_user_id` 判定
- **本次变更(2026-10-12 第四轮:「设置里的同步邮箱」收敛到外部邮箱)**:
  - `_recipients(session)`(`:135`)现在**只返回外部行**(`user_id IS NULL`):这张可操作的表格
    里再也没有账号收件人,增删启停都只影响外部邮箱;
  - 新增 `_account_recipient_entries(session)`(`:150`)与上下文键 `account_recipients`:
    逐个账号给出 `{user, email, verified, deliverable, status}`(`status` ∈
    `自动同步中` / `邮箱待验证` / `未填邮箱` / `账号已停用,不收信`),
    模板把它渲染成下方**只读**的「账号邮箱(自动同步)」表;
  - 新增 `_guard_external_recipient(recipient, action)`(`:180`):账号行返回中文提示,
    外部行返回 `None`。`toggle_recipient` 与 `remove_recipient` 命中账号行时
    **409** + 提示「这是账号邮箱,自动跟随成员资料,不能在这里…;要停收信请到成员页停用该账号」
    (此前的页面删除路径没有任何守卫,管理员可以在设置页把成员的账号收件人直接删掉);
  - `_recipient_partial` 与 `settings_page` 在渲染前调一次
    `app/accounts.py::sync_account_recipients`(有变化即提交):页面上的「谁在收信」
    与邮件队列看到的是同一份事实,历史库里被删/停用的账号行会自动补回来;
  - `GET /admin/settings` 的上下文新增 `account_recipients`
- **测试**:`tests/test_family_pages.py::test_recipient_partial_shows_scope`
  (只覆盖「本人可见日历」与「家庭共享日历」两种文案,`scope_user_id` 支线未覆盖)、
  `::test_recipient_api_reports_scope_and_blocks_account_rows`、
  `tests/test_account_recipient_sync.py::test_settings_page_cannot_remove_or_disable_an_account_recipient`
  (页面删除/停用账号行 → 409,行仍然启用)、
  `::test_recipients_partial_lists_account_mail_as_read_only`
  (账号邮箱出现在只读列表、且页面里没有它的 toggle/delete 表单)

### 权限矩阵(成员 vs 管理员)

| 入口 | 成员 | 管理员 | 依赖 |
|---|---|---|---|
| `/admin`、`/admin/events/new`、`/admin/events/{id}` | ✅ | ✅ | `UserDep` |
| `/admin/me`、`/admin/partials/me`、`/admin/partials/feeds` | ✅(仅自己) | ✅(仅自己) | `UserDep` / `CsrfDep` |
| `/admin/actions/profile`、`/admin/actions/password` | ✅ | ✅ | `CsrfDep` |
| `/admin/members`、`/admin/partials/members`、`/admin/partials/invites` | ❌ 403 | ✅ | `AdminDep` / `CsrfAdminDep` |
| `/admin/settings`、`/admin/sync`、`/admin/partials/settings`、`/admin/partials/sync` | ❌ 403 | ✅ | 同上 |
| `/admin/partials/recipients`、`/admin/partials/tokens`、`/admin/partials/family-feeds` | ❌ 403 | ✅ | 同上 |
| `/admin/actions/host-port-check` | ❌ 403 | ✅ | `CsrfAdminDep`(2026-10-14 新增) |

- **不变量**:成员被拒时是 **403**(`app/deps.py::Forbidden`,由 `app/main.py` 统一转成
  HTML 页或 `{"error": {...}}`);越权读取**事件**则是 **404**。两者不要混用。
  上面的路径在本机是裸路径,从 `/calendar/...` 进来时由 `app/main.py` 再挂一份,
  同一套判定(见 `docs/static/app-main.md`)
- **测试**:`tests/test_family_pages.py::test_member_is_locked_out_of_admin_surfaces`、
  `::test_member_cannot_use_admin_apis`、`::test_member_nav_hides_admin_links`、
  `tests/test_family_web.py::test_admin_only_pages_reject_members`

### `GET /admin/sync`(`sync_page`)

- **位置**:`app/routers/pages.py:1289`(函数 `:1290`);依赖 `AdminDep`(成员访问 403)
- **用途**:ICS 版本与邮件队列(`partials/sync_status.html`)
- **不变量**:这里显示的是**全局并集版本**——任何成员的改动(包括改自己私人日历里的
  一条日程)都会推进同一个 `export_version` 并重发邮件;页面上没有「只重新生成某个人」
  这种操作,因为版本号与 `content_hash` 只由全库并集决定
- **测试**:`tests/test_family_pages.py::test_admin_pages_render`、
  `tests/test_family_web.py::test_admin_only_pages_reject_members`

### 订阅地址的服务范围(与本文档强相关的不变式)

页面只负责**创建与撤销**令牌,实际服务范围由 `app/routers/feed.py` 决定,
但两边的语义必须一致,所以在这里一并写明:

| 入口 | 服务范围 | 备注 |
|---|---|---|
| `/calendar/<令牌>.ics`(`user_id` 非空) | 该账号的可见范围(个人 + 共享) | 文件是 `export/feeds/user-<id>.ics` |
| 同上(`user_id` 为空的**家庭令牌**) | 只有家庭共享日历 | 文件是 `export/feeds/family.ics` |
| 每个账号的**固定**地址 | 同上,但令牌由服务端密钥派生、永不变更 | 界面不给停用/撤销(409),可长期显示与重发 |
| `/calendar`、`/calendar/`(前缀本身) | **不再提供日历** | 机器 404 纯文本 + `no-store`,浏览器 303 → `/calendar/admin` |
| `/calendar.ics` | **不再提供日历** | 落到 `feed.py` 的令牌路由 → 解析不到 → 404 纯文本 |

- **不变量**:旧的无令牌地址**不是**「首位管理员的可见范围」那种兼容别名,
  更**绝不是全库并集** —— 它只是 404;任何对外地址都不得直接服务
  `export/calendar.ics`(内部一致性产物)
- **测试**:`tests/test_family_web.py::test_owner_fixed_address_serves_owner_scope_not_union`、
  `::test_legacy_tokenless_address_is_gone`、
  `::test_member_feed_contains_only_their_visible_events`、
  `::test_family_feed_excludes_personal_calendars`、
  `::test_invalid_feed_token_returns_404`、`::test_disabled_feed_token_returns_404`、
  `::test_feed_token_only_sees_its_own_file`、
  `tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`

### 一次性明文展示规则

同一套规则用于下面这些敏感值,页面与库的行为必须保持一致:

| 值 | 创建入口 | 渲染位置 | 明文可见范围 |
|---|---|---|---|
| 远程接入令牌 | `POST /admin/partials/tokens` | `partials/tokens.html` `new_token` | 仅该次响应 |
| 个人**临时**订阅令牌 | `POST /admin/partials/feeds` | `partials/feeds.html` `new_token` | 仅该次响应 |
| 家庭订阅令牌 | `POST /admin/partials/family-feeds` | `partials/family_feeds.html` `new_token` | 仅该次响应 |
| 邀请码 | `POST /admin/partials/invites` | `partials/members.html` `new_invite` / `new_invite_url` | 仅该次响应 |
| 成员临时口令 | `POST /admin/partials/members/{id}/reset` | `partials/members.html` `new_password` | 仅该次响应 |

- **规则**:渲染函数收到 `new_token` / `new_invite` / `new_password` 参数时才会显示;
  成功响应之后的任何一次列表渲染都必须传 `None`,列表列只展示
  `token_hash[:16]` / `invite.token_hash[:16]` 指纹或用户名
- **唯一的例外是账号的固定订阅地址**:它由服务端密钥派生(`account_feed_token`),
  因此页面**可以长期显示**(`fixed_feed_url`),也允许"忘记了自己再看一眼";
  但库里仍然只有 `token_hash`,明文不得写进日志或审计 detail
  (`AGENTS.md` 不变量 18 与 23)
- **不变量**:其余明文不得进入日志、审计 detail 或数据库;页面只显示指纹
- **测试**:`tests/test_family_pages.py::test_agent_token_plaintext_is_shown_only_once`、
  `::test_my_feed_is_stable_and_temporary_token_shown_once`、
  `::test_invite_plaintext_is_shown_once_and_register_link_works`、
  `::test_reset_member_password_shows_one_time_password_once`

## 新增(2026-10-14:对外地址卡片与宿主端口探测)

设置页新增「对外地址(Tailscale)」区块(`templates/partials/settings_form.html:64` 起)。
根约束不变:**界面里只能改 origin**,路径前缀是启动时挂载路由用的
(`app/main.py`),运行时改它会让链接与路由错位,所以库里只存 origin、完整地址由
`app/settings_store.py::effective_settings` 拼出来。宿主端口的只读探测见
[`app-host-port.md`](app-host-port.md)。

### `_tailnet_origin_hint(request, settings) -> str`

- **位置**:`app/routers/pages.py:1784`
- **用途**:从**本次请求**推导「对外地址」的候选 origin,用于设置页的「自动填入」
- **返回**:
  - `f"{scheme}://{name}"`,其中 `name` 取自 `Host` 头去掉端口后的部分(转小写),
    `scheme` 取 `X-Forwarded-Proto` 的第一段;**取不到转发头时按
    `settings.public_base_url` 的协议兜底**(是 `https://` 就 `https`,否则 `http`),
    而不是硬猜 `https`;
  - **空串**:`Host` 为空、`Host` 以 `[` 开头(IPv6 字面量)、或 `name` 不以
    `.ts.net` 结尾
- **参数**:`settings` 只用于上面那条「协议兜底」;真正决定路径前缀的地方仍是 `app/urls.py`
- **副作用**:无(只读请求头)
- **不变量**:
  - `Host` 与 `X-Forwarded-Proto` **只是建议来源,不是安全判据** —— 值要管理员确认保存后
    才写库,而且只允许写到 `*.ts.net` 上;
  - `localhost`、内网 IP、IPv6 一律**不猜**(服务端无从知道你的节点名),
    页面改为提示手动填写;
  - 这里**不**校验、不规范化,真正的把关是
    `app/settings_store.py::normalize_public_base_origin`
- **测试**:`tests/test_settings_public_base_url.py::test_tailnet_visit_is_auto_filled_but_localhost_is_not`
  (带 `host: sisi-laptop.tail921448.ts.net` 的片段里出现「已按你当前的访问地址自动填入」与
  `https://sisi-laptop.tail921448.ts.net`;带 `host: localhost:443` 的片段里出现「请手动填写」)

### `_serve_command(settings) -> str`

- **位置**:`app/routers/pages.py:1805`
- **用途**:生成给**宿主机**复制粘贴的 `tailscale serve` 命令(应用在容器里、非 root,
  不碰整机配置,只生成文本)
- **返回**:
  - 有挂载前缀:`tailscale serve --bg --yes --https=443 --set-path=/calendar http://127.0.0.1:<HOST_PORT>/calendar`
  - 无前缀:省略 `--set-path`:`tailscale serve --bg --yes --https=443 http://127.0.0.1:<HOST_PORT>`
- **参数**:端口取自 `settings.host_port`(`HOST_PORT`),路径取自
  `urls.public_base_path(settings)`
- **副作用**:无(纯字符串拼接)
- **不变量**:端口**不得**写死 443 —— `compose.nas.yaml` 发布的是 18443,
  照抄文档就会生成一条打不通的命令;`--set-path` 与目标 URL 的路径必须**逐字相同**
  (本仓库不依赖前缀剥离,见《部署与运维》第 3 节)
- **测试**:`tests/test_settings_public_base_url.py::test_serve_command_follows_host_port_and_prefix`
  (`host_port=18443` 与前缀 `/calendar` 的完整字符串断言;无前缀时省略 `--set-path`)、
  `::test_settings_card_shows_source_prefix_and_serve_command`(片段里确实渲染出该命令)

### `_settings_form_context(request, settings, overrides) -> dict[str, object]`

- **位置**:`app/routers/pages.py:1818`
- **用途**:`templates/partials/settings_form.html` 的上下文
- **为什么必须共用**:`settings.html` 整页是 `{% include "partials/settings_form.html" %}`,
  而 HTMX 保存后只换这一个片段 —— 两条渲染路径必须给出**同一套变量**。
  模板开了 `StrictUndefined`,少一个变量就是 **500**(这是本轮真实踩到并修掉的坑,
  所以上下文被抽成这一个函数)
- **返回的键**:
  - `settings`、`overrides`(模板用它回填其余字段)
  - `public_base_url_effective`:`urls.public_base_url(settings)`(**当前生效值**,
    已含挂载前缀)
  - `public_base_url_env`:`urls.public_base_url(env_settings)`,其中
    `env_settings = getattr(request.app.state, "settings", settings)`
    —— **进程启动时的基线 `Settings`**(`app/main.py::create_app` 收到的那一份,
    默认 `get_settings()`,即环境变量 / `.env`,**不含**库里的覆盖),
    页面据此显示「来自环境变量」的来源
  - `public_base_url_override`:库里那条 origin(`overrides.get(PUBLIC_BASE_URL_KEY, "")`),
    非空时页面显示「(来自界面覆盖)」
  - `public_base_url_input`:输入框的 `value`,优先级 = 库里的 origin → 自动检测到的候选 →
    环境变量的完整地址
  - `public_base_url_suggested`:`_tailnet_origin_hint(request, settings)`,但**已有覆盖时为空**
    (不覆盖用户自己的选择)
  - `public_base_path`:`urls.public_base_path(settings)`,页面用它写「路径前缀只读说明」
  - `host_port`:`settings.host_port`,页面上直接显示
  - `serve_command`:`_serve_command(settings)`
- **副作用**:无
- **不变量**:这是设置表单**唯一**的上下文来源;任何一条渲染路径都不得自己再拼一套
  —— 尤其不许在整页里少喂 `serve_command` / `host_port` 这类新键
- **测试**:`tests/test_settings_public_base_url.py::test_settings_card_shows_source_prefix_and_serve_command`
  (断言「对外地址(Tailscale)」「当前生效值」「来自环境变量」「tailscale serve status」
  与 `pages._serve_command(...)` 都在片段里)、
  `::test_tailnet_visit_is_auto_filled_but_localhost_is_not`、
  整页侧由 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined` 兜住

### `_settings_form_response(request, session, settings, admin, *, notice, error, status_code=200) -> Response`

- **位置**:`app/routers/pages.py:1846`
- **用途**:渲染 `partials/settings_form.html`(HTMX 保存/校验失败后的那一片,以及
  `GET /admin/partials/settings`)
- **本次变更**:上下文改为抽出 `_settings_form_context(request, settings, overrides)`,
  再叠加 `csrf_token`、`**_timezone_context(settings.default_timezone)`、`notice`、`error`;
  `status_code` 由调用方给(校验失败是 422)
- **副作用**:无(只读 `load_overrides`)
- **不变量**:它给出的键必须与 `settings_page` 的整页上下文**逐字一致**
- **测试**:`tests/test_settings_public_base_url.py::test_saving_the_card_field_updates_the_effective_url`
  (表单 POST 后片段里出现新的生效地址)、
  `tests/test_api_settings.py::test_settings_form_partial_updates_and_validates`

### `POST /admin/actions/settings`(`save_settings_action`)

- **位置**:`app/routers/pages.py:1866`(函数 `:1867`);依赖 `CsrfAdminDep`
- **表单**:原有五项 + **`public_base_url: str = Form(default="")`**(`app/routers/pages.py:1876`)
- **本次变更**:`values` 里多一个 `"public_base_url"`(`:1884`–`:1885`),
  并改为 `save_overrides(session, values, effective_settings(session))`(`:1888`)
  —— 把**生效设置**传进去,取挂载前缀时用的是已经叠加过覆盖的那一份
- **空值语义**:表单字段留空 = **撤销覆盖**、回到环境变量(`save_overrides` 删行);
  路径写法非法时 422 并回显文案
- **副作用**:写库 + 审计 `settings_save/success`;失败回滚
- **不变量**:保存后**不需要重启** —— 邀请链接、订阅地址、邮件正文里的地址都在下次
  渲染时按新的 `Settings.public_base_url` 派生
- **测试**:`tests/test_settings_public_base_url.py::test_saving_the_card_field_updates_the_effective_url`、
  `::test_api_accepts_the_origin_and_returns_the_composed_url`

### `POST /admin/actions/host-port-check`(`host_port_check_action`)

- **位置**:`app/routers/pages.py:1908`(函数 `:1911`);依赖 `CsrfAdminDep`(登录 + CSRF + 管理员)
- **用途**:探测宿主端口映射是否真的通了,把结果渲染成一个片段
  (`templates/partials/host_port_check.html`,目标 `#host-port-result`)
- **行为**:取 `effective_settings(session)` → `host_port.probe_host_port(settings)` →
  审计 `host_port_check`(`success` / `failed`)→ 渲染片段
- **上下文**:`reachable`、`detail`(中文说明,含修法)、`host_port`
- **副作用**:一次**只读**宿主探测 + 一条审计;不写配置、不改库
- **不变量**:
  - **只在管理员点击时跑** —— 页面加载不会自动探测(它是同步阻塞的,2 秒超时);
  - 探测目标由 `app/host_port.py` 写死(主机名 `host.docker.internal`、路径 `/healthz`、
    端口只认整数),路由不额外传入任何目标;
  - 按钮在模板里是 `type="button"`,**不是**提交按钮 —— 点它不会顺带保存设置
- **测试**:`tests/test_settings_public_base_url.py::test_host_port_check_endpoint_returns_a_partial`
  (monkeypatch 探测函数,断言片段里出现替身文案与 `host.docker.internal`)

## 找不到对应测试的点

- `POST /admin/actions/profile` 的「验证码邮件发不出去」分支(notice 文案不同)没有专门测试。
- `public_root` 的 **HEAD** 方法没有单独用例(`app/main.py` 用 `methods=["GET", "HEAD"]`
  挂载,而 `tests/test_public_prefix.py` 的断言都走 GET)。
- 邀请码片段的纯明文键 `new_invite` 已不再被模板使用(只有 `new_invite_url` 被渲染);
  「多喂一个键」这件事没有测试,它现在只是 `_members_context` 的默认值。
- 整页 `/admin/me` 与 `/admin/members` 的上下文已由 `_me_context` / `_members_context`
  共用,并由 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined` 兜住;
  但那条用例只保证「不缺键」,**没有**断言整页里确实出现了固定地址输入框或验证提示文案。
- `_tailnet_origin_hint` 的 IPv6 分支(`Host` 以 `[` 开头)与「协议兜底」分支由
  `tests/test_settings_public_base_url.py::test_tailnet_hint_scheme_falls_back_to_the_configured_base`
  覆盖;「`Host` 带端口」分支由 `::test_tailnet_visit_is_auto_filled_but_localhost_is_not`
  (`localhost:443`)间接覆盖。
- `_settings_form_context` 的 `public_base_url_env`(环境变量**原值**,与生效值的区别)
  没有专门断言,只有「来自环境变量」/「(来自界面覆盖)」这两句文案被间接覆盖。
- `POST /admin/actions/host-port-check` 只覆盖了探测**成功**的替身分支;
  失败时 `.alert error` 的渲染与审计 `failed` 都没有断言。

## 变更记录

- 2026-09-29:新增 `_timezone_context`;`COMMON_TIMEZONES` 改为清单派生;
  邮件队列上下文补充 `default_timezone`。
- 2026-09-29:新增远程接入令牌管理片段(`#agent-tokens`)与 `settings_page` 的 `tokens` 上下文。
- 2026-09-30:日程新建/编辑页上下文补充 `reminder_options` 与 `custom_reminders`。
- 2026-10-08:家庭版改造 —— `_base_context`(current_user/is_admin/个人时区)、`_csrf_of`、
  `_calendar_context`;新增 `/admin/me`、`/admin/members` 页面与
  `partials/me|feeds|family_feeds|members|invites` 片段,以及成员管理、邀请码、
  共享日历权限、个人资料/改口令、个人与家庭订阅令牌的全部路由;
  事件编辑页越界改 404 且引入只读态;权限矩阵与一次性明文规则见上。
  同时把全文行号按当前文件校正(旧条目引用的 `:75`/`:97`/`:472`/`:754` 等已失效)。
- 2026-10-11:邮件收件人片段的「范围」列扩为三种显示(本人可见日历 / 沿用某成员的可见日历 +
  历史绑定 / 家庭共享日历),片段上下文未变;`scope_user_id` 支线的 UI 暂无测试覆盖。
- 2026-10-12:公开前缀与固定地址。新增 `FEED_PLACEHOLDER_TEXT`、`public_root()`
  (前缀本身:机器 404 纯文本 + `no-store`、浏览器 303 → `/admin`)与 `GET /` 的
  `root()`(裸根跳管理页、前缀根交给 `public_root`);`_base_context` 新增
  `public_base_url`;`_me_partial` 新增 `fixed_feed_url` / `fixed_token_id` / `verify_state`,
  `_feed_partial` 新增固定地址三件套、toggle/delete 对固定行返回 **409**;
  `save_profile_action` 改为"邮箱真变了才 `mark_unverified` + 发新验证码";
  `create_invite_action` 用 `urls.invite_url()` 生成邀请链接,`_members_context` 给出
  `invite_base_url` / `public_base_url`;订阅范围表把旧 `/calendar` 口径改为「不再提供日历」
  并补固定地址一行。全文行号按当前源码重校,并把
  `test_my_feed_token_plaintext_is_shown_only_once` 更正为
  `test_my_feed_is_stable_and_temporary_token_shown_once`、
  `test_profile_update_creates_mail_recipient` 更正为 `test_profile_update_starts_email_verification`。
- 2026-10-12(同日后补):抽出 `_me_context(session, settings, current)`(`:391`)——
  (本条与下一条里的行号是**当时**的值,最新行号见 2026-10-14 条)
  整页 `/admin/me` 与片段 `/admin/partials/me` 从此共用同一套「我的设置」上下文
  (此前整页缺 `feed_tokens` / `fixed_feed_url` / `fixed_token_id` / `verify_state` / 时区候选,
  Jinja 的默认 `Undefined` 只会安静地渲染成空态);`_me_partial` 的参数与模板键统一为
  `new_token`(旧名 `new_feed_token` 已不存在);`_members_context` 给四个一次性明文键
  (`new_invite` / `new_invite_url` / `new_password` / `reset_username`)补 `None` 默认值。
  行号按再校后的源码更新(`_me_partial` `:428`、`me_page` `:460`、`me_partial` `:478`、
  `save_profile_action` `:488`、`change_password_action` `:538`、`_feed_partial` `:577`、
  `settings_page` `:1191`、`_sync_partial` `:1953`、`__all__` `:2003` 等)。
  新增兜底测试 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined`
  (把 `templates.env.undefined` 换成 `StrictUndefined`,上下文缺键当场报错)。
- 2026-10-12(第四轮:「设置里的同步邮箱」收敛到外部邮箱):`_recipients` 只返回外部行,
  新增 `_account_recipient_entries`(只读的账号邮箱列表)与 `_guard_external_recipient`
  (toggle/delete 命中账号行 → 409);`_recipient_partial` 与 `settings_page` 渲染前
  调 `sync_account_recipients` 对齐并提交;`account_recipients` 进入片段与整页上下文。
  测试:`tests/test_account_recipient_sync.py` 的两条页面用例。
- 2026-10-14:设置页新增「对外地址(Tailscale)」卡片与宿主端口探测。新增
  `_tailnet_origin_hint`(`:1784`,只从 `Host`/`X-Forwarded-Proto` 推导候选且**只认
  `*.ts.net`**,localhost/IPv6 一律不猜)、`_serve_command`(`:1805`,端口取
  `HOST_PORT`、路径取挂载前缀,`--set-path` 与目标 URL 逐字相同)、
  `_settings_form_context`(`:1818`,整页 `settings.html` include 与 HTMX 局部渲染**共用**
  同一套变量 —— 模板开了 `StrictUndefined`,少一个键就是 500)与
  `_settings_form_response`(`:1846`,改为展开该上下文);
  `settings_page` 上下文同样改为 `**_settings_form_context(...)`(`:1258`);
  `save_settings_action` 新增表单字段 `public_base_url`(`:1876`)并改用
  `save_overrides(session, values, effective_settings(session))`(`:1888`,空值 = 撤销覆盖);
  新增 `POST /admin/actions/host-port-check`(`host_port_check_action` `:1911`,
  `CsrfAdminDep`,审计 `host_port_check`,渲染 `partials/host_port_check.html`)。
  权限矩阵补该入口一行;全文行号按当前源码重校
  (`FEED_PLACEHOLDER_TEXT` `:245`、`public_root` `:251`、`root` `:270`、
  `_timezone_context` `:119`、`_base_context` `:198`、`_csrf_of` `:219`、
  `_calendar_context` `:224`、`_me_context` `:442`、`settings_page` `:1243`、
  `sync_page` `:1289`、`_recipient_partial` `:1313`、`_token_partial` `:1477`、
  `_family_feed_partial` `:1666`、`_sync_partial` `:2130`、
  `settings_form_partial` `:2169`、`__all__` `:2180` 等)。
  测试:`tests/test_settings_public_base_url.py`(25 个用例)。