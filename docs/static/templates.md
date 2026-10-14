# `templates/`

Jinja2 模板。约定**:不使用任何内联脚本**——管理页启用了严格 CSP
`script-src 'self'`(见 `docs/static/app-main.md`),因此没有 `<script>…</script>` 正文、
也没有 `onclick=` 之类的事件属性;所有前端逻辑放在 `static/js/` 里,
需要传给 JS 的值一律通过 `data-*` 属性,勾选类控件通过 `value` / `checked` 传递。
约定的上下文键来自 `app/routers/pages.py`(见 `docs/static/app-routers-pages.md`)。

**2026-10-12 起所有模板里的站内链接都不再写死路径**,一律用 Jinja 全局函数 `bp()`
(`app/deps.py:65` → `app/urls.py::current_prefix`,返回**当次请求**绑定的前缀,
本机是空串、经 Tailscale 进来是 `/calendar`);表单 `action`、`hx-post`、`href` 同理。
带路径的绝对地址(家人入口、邀请链接、订阅地址)用上下文里的 `public_base_url`
或 `urls.invite_url()` / `urls.subscription_url()`,**不要**自己拼字符串
(见 `docs/static/app-urls.md`)。

## 模板清单(家庭版)

```text
templates/
├─ base.html                  导航(按 is_admin 分级)+ 用户胶囊 + 退出表单
│                             所有链接经 bp();<body data-base-path> 给 JS 用
├─ calendar.html              日历页(左侧「我的日历」侧栏)
├─ event_form.html            新建/编辑日程(日历下拉、提醒、只读态)
├─ login.html                 登录(含「去注册」入口)
├─ register.html              邀请码注册(邮箱必填)
├─ verify_email.html          邮箱验证(验证码 / 重发 / 改地址 / 退出)
├─ bootstrap.html             首位管理员引导码设口令(初始口令救援,自带 HTML shell)
├─ me.html  members.html  settings.html  sync.html        各整页
└─ partials/
   ├─ me.html                 资料 / 改口令 / 我的订阅地址(含验证状态块)
   ├─ feeds.html              个人订阅令牌:固定地址 + 一次性临时明文
   ├─ family_feeds.html       家庭订阅令牌(管理员)
   ├─ members.html            邀请码 / 账号 / 共享日历与权限
   ├─ recipients.html         外部收件人(可维护)+ 账号邮箱(只读,自动同步)
   ├─ tokens.html             远程接入令牌
   ├─ settings_form.html      全局设置
   ├─ sync_status.html        版本与邮件队列(含公开面基地址)
   └─ mail_status.html        邮件状态提示
```

## `templates/base.html`

- **变更(2026-10-12,静态资源加指纹)**:
  - CSS/JS 一律改用 Jinja 全局 `asset('css/app.css')`(`app/deps.py` 注册 →
    `app/urls.py::asset`),渲染成 `/static/css/app.css?v=<大小>-<mtime>`。
    **动机**:浏览器会长期缓存静态文件,地址不变时改完样式用户看到的还是旧版
    (真机反馈的「侧栏错版」就是这个);带指纹后部署新镜像即失效旧缓存。
    涉及 `templates/base.html`(app.css、htmx、copy.js)、`calendar.html`(FullCalendar 与
    `calendar.js`)、`event_form.html`(`event_form.js`)与三个独立页
    (`login.html`/`register.html`/`verify_email.html` 各自也有 `<link>`)
  - 新增 `static/js/copy.js`(`defer` 加载,见下文「`static/js/copy.js`」一节)
- **变更(2026-10-12)**:
  - **站内链接全部改为 `{{ bp() }}/...`**(导航五项、退出表单、登录按钮)。本机访问时
    `bp()` 是空串、经 `/calendar` 访问时是 `/calendar`,同一份模板两边都对
    (见 `docs/static/app-urls.md`)
  - **`<body data-base-path="{{ bp() }}">`**(`templates/base.html:11`):把前缀交给 JS,
    `static/js/calendar.js` 与 `static/js/event_form.js` 都用它拼接口与跳转地址
    (读不到时退回空串,即裸路径)
  - 页脚改成两行绝对地址:「本机管理地址 `http://localhost:443/admin`」写死;
    「家人入口 `<code>{{ public_base_url }}/login</code>`」取上下文里的 `public_base_url`。
    **模板里已经没有任何 `subscription_url` 引用**(旧键已被 `public_base_url` 取代)
- **变更(2026-10-08)**:
  - 导航按角色分级:所有成员都看到「日历」「我的设置」;
    `{% if is_admin %}` 时才追加「成员」「设置」「同步状态」三个入口。
    成员页面里**不得**出现 `/admin/members`、`/admin/settings`、`/admin/sync` 的链接
  - 右上角新增 `.user-box`:`current_user.label`、`current_user.username`、
    角色胶囊(`管理员` / `成员`);**未登录时只给一个「登录」按钮**,
    退出表单放在 `{% if current_user %}` 里(未登录时 `csrf_token` 是空串,没有意义)
- **上下文**:`is_admin`、`current_user`、`csrf_token`、`active`、`calendar_name`、
  `public_base_url`、`system_timezone`(整页模板里的兜底默认时区)
- **不变量**:角色分级只靠 `is_admin` 这一个布尔值,模板不要自己判断 `current_user.role`;
  链接**不得**出现写死的 `/admin`、`/login`、`/logout` 前缀,静态资源同理
- **测试**:`tests/test_family_pages.py::test_member_nav_hides_admin_links`、
  `::test_admin_pages_render`、`::test_member_pages_render`、
  `tests/test_public_prefix.py::test_admin_pages_under_prefix_link_within_the_prefix`
  (带前缀渲染时页面里不出现裸 `href="/admin"`)、
  `::test_base_url_without_path_falls_back_to_bare_paths`

## `templates/calendar.html`

- **变更(2026-10-12,侧栏排版重做)**:真机反馈「新建日程那块太丑」——原来是一个贴边的
  纯色长条 + 紧随其后的裸 `<span>` 小标题。现在:
  - 主按钮加 `.sidebar-cta`(上下留白、图标 + 文案)与内联 `<svg class="icon" width="14"
    height="14">` 的「+」;**SVG 必须写死宽高**,否则 CSS 失效时会撑满整行
  - 「显示哪些日历」升级为 `.sidebar-label`(12px 灰字 + 上边距),日历清单包在带边框圆角的
    `.calendar-list` 里、行间用 `li + li` 细线分隔(`.calendar-item` 不再自带圆角)
  - 底部提示缩短为一句「共享日历默认只读;管理员可放开写权限,放开后才能拖动改期或删除。」
- **变更(2026-10-08)**:
  - 由单列改为 `.calendar-layout` 两栏:左侧 `<aside class="calendar-sidebar">`,
    右侧仍是 `#calendar` 宿主
  - 侧栏「我的日历」:遍历 `calendar_options` 生成
    `<input type="checkbox" name="calendar-filter" data-calendar-id="{{ item.id }}" checked>`,
    每行带 `.cal-dot`(背景色取 `item.color`)、日历名、`个人`/`共享` 胶囊,
    以及 `{% if not item.can_write %}<span class="chip warn">只读</span>{% endif %}`
  - 「新建日程」按钮:`can_write_anything` 为真时是链接
    (`/admin/events/new?calendar={{ default_calendar_id }}`),否则是 `disabled` 按钮 + 提示文案
  - 日历宿主新增 `data-calendars='{{ calendar_options | tojson }}'`,
    供 `static/js/calendar.js` 解析出 id → 名称/颜色/可写的映射
- **上下文**:`calendar_options`、`default_calendar_id`、`can_write_anything`、`csrf_token`、
  `default_timezone`、`sync_state`、`recurrence_labels`、`status_options`
- **前端协作(无内联脚本)**:
  - `static/js/calendar.js::parseCalendarOptions` 读 `host.dataset.calendars`
  - `selectedCalendarIds()` 读每个勾选框的 `dataset.calendarId`,拼成
    `calendar_id` 查询参数(可重复)交给 `GET /api/v1/events`;
    一个都没勾选时**不请求后端**,直接 `success([])`
  - 事件颜色优先用 `item.calendar_color`(接口返回),回退到 `data-calendars` 里的颜色
  - 拖动改期前检查 `extendedProps.can_write`,`false` 时 `info.revert()` 并提示
- **不变量**:勾选框默认全选;「至少保留一个」只由 JS 提示,**服务端不依赖**这个状态
  (接口对空 `calendar_id` 集合返回空结果,见 `docs/static/app-events.md`)
- **测试**:`tests/test_family_pages.py::test_calendar_page_exposes_calendars_for_the_sidebar`、
  `::test_sidebar_primary_action_is_a_proper_button`(主按钮带 `sidebar-cta` 与图标、
  清单有 `sidebar-label` + `calendar-list`)、`::test_member_pages_render`

## `templates/event_form.html`

- **变更(2026-10-08)**:
  - 表单顶部新增两条只读提示:`editing and not event_writable` → 「你没有修改该事件的权限
    (家庭共享日历默认只有管理员能改)」;`not editing and not event_writable` →
    「你没有可写的日历,暂时无法新建日程」
  - 新增「日历」下拉:`{% set writable_calendars = calendar_options | selectattr('can_write') | list %}`,
    只渲染**有写权限**的日历,`value` 是日历 id,选中项取 `selected_calendar_id`;
    一个都没有时不渲染下拉,只给提示
  - 保存按钮:`{{ '' if event_writable else 'disabled' }}`;删除按钮的条件由
    `{% if editing %}` 收紧为 `{% if editing and event_writable %}`
  - 底部提示语改为「把**你可见日历里尚未结束的日程**快照发给对应的收件人
    (每人只收到自己范围的内容)」;只读态改为「查看模式:保存与删除都已停用」
- **上下文**:`calendar_options`、`selected_calendar_id`、`event_writable`、`reminder_options`、
  `custom_reminders`、`timezone_choices`、`timezones`
- **前端**:`static/js/event_form.js::buildPayload` 把 `calendar_id` 放进 payload,
  只在非空时带上(编辑时用于迁移日历,新建时决定落库日历);
  只读表单的提交按钮已 `disabled`,提交事件直接 early-return
- **不变量**:下拉里**只**出现 `can_write` 为真的日历;这是 UX 兜底,真正的判定仍在
  `GET /admin/events/{id}`(`event_writable`)与服务端 `can_write()`
- **测试**:`tests/test_family_pages.py::test_event_form_for_shared_calendar_is_read_only_for_member`、
  `tests/test_reminders.py::test_event_form_renders_reminder_controls`

### 新增:提醒与链接字段(2026-09-30)

- **提醒块**:`<fieldset class="field reminder-field">` + `<div class="checkbox-group" data-reminders="1">`
  - 遍历 `reminder_options` 渲染 `<input type="checkbox" name="reminder_preset" value="{{ item.minutes }}">`,
    按 `item.selected` 输出 `checked`
  - 自定义分钟:`<input type="text" name="reminder_custom" id="reminder-custom">`,值来自 `custom_reminders`
  - 提示文案说明「相对事件开始时间」「全天事件以 00:00 为基准」「会写进 `VALARM`」
- **链接字段**:`<input type="url" name="url" maxlength="1000" value="{{ event.url or '' }}">`
- **上下文**:`reminder_options`(`app/reminders.py::preset_options`)、`custom_reminders`
  (`custom_reminder_text`)
- **前端**:`static/js/event_form.js::collectReminders` 把勾选项与自定义文本合成 `reminders` 数组
  (去重、从大到小);`static/js/calendar.js` 在事件 tooltip 里显示「提醒:…」与「链接:…」
- **不变量**:不使用内联脚本(严格 CSP),勾选值通过 `value` 属性传递
- **测试**:`tests/test_reminders.py::test_event_form_renders_reminder_controls`、
  `::test_edit_form_marks_existing_reminders`

## `templates/partials/sync_status.html`

- **变更**:邮件队列表格新增两列
  - **操作时间**:`delivery.created_at`,即任务创建时刻,`title` 给出到秒的时间与时区
  - **发送时间**:`delivery.sent_at`,未发送显示 `—`
  - 「下次重试」由 `strftime` 改为同一套 `localtime` 过滤器,三个时间列显示口径一致
  - 表格上方 `<p class="hint">` 注明「操作时间为任务创建时刻;时间均按日历默认时区 X 显示」
- **列顺序**(2026-10-12 起):版本 / 收件人 / 类型 / **发起人** / 状态 / 尝试 / 操作时间 /
  发送时间 / 下次重试 / 错误 / 操作
- **上下文**:`deliveries`、`default_timezone`(新增)、`sync_state`、`status_labels`、`change_labels`、`csrf_token`
- **过滤器**:`localtime`(见 `docs/static/app-deps.md`)
- **测试**:`test_mail_queue_shows_operation_and_sent_time`
- **2026-10-06 变更**:「向当前人员发送最新版」按钮的 `hx-confirm` 文案由
  「发送完整日历快照」改为「发送尚未结束的日程快照(已结束的日程不在附件里)」——与实际附件内容一致
  (见 `docs/static/app-mailqueue.md`)。表格结构未变。
- **2026-10-08 变更**:无结构变化;`/admin/sync` 与 `#sync-status` 的访问面收窄到管理员
  (成员访问 403),片段本身未加角色判断
- **2026-10-12 变更**:`<dl class="kv">` 里的一行由旧键改为
  `<dt>公开面基地址</dt><dd class="mono">{{ sync_state.public_base_url }}</dd>`
  (`templates/partials/sync_status.html:23`)。**模板里已无 `subscription_url` 引用**
  (该键在 `app/schemas.py::SyncStatusRead` 里被重命名为 `public_base_url`,
  见 `docs/static/app-schemas.md`);其余九行(版本 / 生成时间 / 内容哈希 / 文件大小 /
  发布文件 / SMTP / 收件人 / 上次成功发送)未变
- **2026-10-12 第二轮变更(发起人列)**:
  - 表头「类型」之后插入 `<th>发起人</th>`,单元格为
    `<td>{{ delivery.actor_label or '—' }}</td>`(`templates/partials/sync_status.html:66`)
  - 数据直接取 `MailDelivery` ORM 对象(`app/routers/pages.py::_deliveries` 返回的就是模型行),
    所以模板里读的是**快照字段** `actor_label`,而不是发信时现查的显示名 —— 队列要还原
    「当时是谁操作的」,发信正文才用现查的名字,两者口径不同是有意的
  - **手动同步显示 `—`**:`CHANGE_MANUAL` 的任务不写发起人正文那一行,但队列里仍会记录
    操作者(备查),这里不区分类型,一律显示记录值
  - **历史任务显示 `—`**:升级前建立的任务 `actor_label` 为空
  - `<p class="hint">` 增补一句「「发起人」是触发这次变更的账号(手动同步不显示)」
  - **不变量**:不得渲染出 `None`(用 `or '—'` 兜底),有
    `tests/test_mail_actor.py::test_queue_page_renders_legacy_tasks_without_an_actor` 守着
  - **测试**:`tests/test_mail_actor.py::test_queue_page_and_api_expose_the_actor`、
    `::test_queue_page_renders_legacy_tasks_without_an_actor`

## `templates/partials/tokens.html`

- **用途**:设置页的「远程接入令牌」卡片内容,HTMX 局部刷新,目标 `#agent-tokens`
- **结构**:
  - `new_token` 非空时显示一次性明文:只读 `<input class="mono token-reveal" readonly>`,
    提示「只显示这一次」——**不使用内联脚本**(严格 CSP 禁止 `onclick`),需手动选中复制
  - 新增表单:`label` + `user_id`(归属账号下拉,默认选中 `users` 里第一位管理员)+
    `calendar_id`(默认日历:`<option value="">个人日历</option>` 加全部共享日历)+
    `acknowledge` 复选框,提交到 `POST /admin/partials/tokens`
  - 列表:名称(可改名)/ 归属账号 / 默认日历 / 指纹 / 状态 / 创建时间 / 操作(停用·启用、撤销)
- **指纹列**:展示 `token_hash[:16]`,与审计日志 `agent:<指纹>` 对得上
- **上下文**:`tokens`、`users`、`shared_calendars`、`csrf_token`、`new_token`、`notice`、`error`
- **测试**:`tests/test_agent_tokens.py` 全文件;
  `tests/test_family_pages.py::test_token_partial_lists_owner_and_shared_calendars`、
  `::test_agent_token_plaintext_is_shown_only_once`

## `templates/partials/feeds.html`(2026-10-08 新增,2026-10-12 加入固定地址与复制按钮)

- **用途**:「我的订阅地址」卡片内容,渲染在 `partials/me.html`(以及整页 `me.html` 对
  `partials/me.html` 的 include)里,目标 `#feeds`
- **结构**:
  - **固定地址卡片**(2026-10-12 新增,`templates/partials/feeds.html:11`–`:24`):
    `{% if fixed_feed_url %}` 为真时渲染 `<input readonly value="{{ fixed_feed_url }}">`,
    文案写明「这条地址在你的账号创建时就已确定,**永远不会改变**」、
    「忘记了自己再看一眼即可」「地址等同于密码」。**它是唯一可以长期明文展示的敏感值**
    (见 `AGENTS.md` 不变量 23)
  - **一键复制**(2026-10-12 第二轮新增):固定地址的输入框带 `id="fixed-feed-url"`,
    旁边是 `<button type="button" class="button small" data-copy="#fixed-feed-url">复制</button>`;
    临时明文块同构(`id="new-feed-url"`)。逻辑全在 `static/js/copy.js`,
    模板里**只有 `data-*`**,没有内联处理器(CSP `script-src 'self'`)
  - **临时地址明文**(`new_token` 非空时):`{{ subscription_prefix }}/{{ new_token }}.ics`
    (只读输入框),提示「**临时订阅地址明文只显示这一次**」「泄露后可以在这里撤销——
    撤销只影响这一条,固定地址不受影响」
  - 新增表单:`label`(设备名,`required`),提交到 `{{ bp() }}/admin/partials/feeds`(生成临时地址)
  - 列表:名称 / 指纹(`token_hash[:16]`)/ 状态 / 最后拉取时间 / 操作;
    **固定那一行**在名称后加 `<span class="chip ok">固定</span>`
    (`:64`–`:66`),操作列换成灰字「不可撤销」(`title` 里写完整理由,
    2026-10-12 第二轮从「固定地址不提供停用或撤销」缩短,否则窄列里会折成两行),
    **不渲染停用/撤销两个表单**
  - 空态(`feed_tokens` 为空,`:101`):「固定地址正在生成,请刷新本页。」
- **上下文**:`feed_tokens`、`new_token`、`subscription_prefix`、**`fixed_feed_url`**、
  **`fixed_token_id`**、`csrf_token`、`default_timezone`、`notice`、`error`
- **不变量**:
  - 明文规则:**临时**地址只出现在创建成功的响应里(`new_token`),列表只显示指纹;
    **固定**地址可以每次渲染都显示(`fixed_feed_url`),但库里仍然只有 `token_hash`;
  - 「固定」判定**必须**用 `fixed_token_id and token.id == fixed_token_id`
    (按 id 比对),**不要**按标签文字猜——标签只是给人看的
    (`app/feed_tokens.py::ACCOUNT_FEED_LABEL`),用户理论上不能改它,但 id 才是事实;
  - 固定行的停用/撤销由**服务端**兜底:即使有人手搓表单直接 POST,
    `toggle_my_feed` / `revoke_my_feed` 也会返回 **409**(见 `docs/static/app-routers-pages.md`)
- **测试**:`tests/test_family_pages.py::test_my_feed_is_stable_and_temporary_token_shown_once`
  (创建响应里固定地址与一次性临时明文同时出现、刷新后固定地址仍在而临时明文不再回显;
  旧名 `test_my_feed_token_plaintext_is_shown_only_once` 已不复存在)、
  `::test_feed_cards_offer_a_copy_button`(片段与整页 `/admin/me` 都要有复制按钮)、
  `::test_new_token_reveals_offer_a_copy_button`、
  `::test_fixed_address_cannot_be_disabled_or_revoked`(直接 POST 停用/撤销各返回 409、
  行仍在且仍启用;**界面无按钮这一条没有断言**)、
  `::test_member_partials_render`(该函数逐个渲染 `/admin/partials/me` 与 `/admin/partials/feeds`)、
  `::test_member_cannot_touch_another_users_feed_token`

## `templates/partials/family_feeds.html`(2026-10-08 新增)

- **用途**:管理员「家庭订阅地址」卡片内容,目标 `#family-feeds`
- **结构**:与 `feeds.html` 同构,但表单默认名是「家庭共享日历」,
  提示明确「这类地址**只包含家庭共享日历**,不含任何人的私人日程」;
  一次性明文的输入框带 `id="new-family-feed-url"` 与
  `data-copy="#new-family-feed-url"` 的「复制」按钮(2026-10-12 第二轮)
- **上下文**:`feed_tokens`(`user_id=None` 的那批)、`new_token`、`subscription_prefix`、
  `csrf_token`、`default_timezone`、`notice`、`error`
- **测试**:`tests/test_family_pages.py::test_admin_partials_render`

## `templates/partials/me.html`(2026-10-08 新增,2026-10-12 加入验证状态块)

- **用途**:`/admin/me` 的面板,目标 `#me-panel`,包含三块
- **结构**:
  - **个人资料**表单(`hx-post="{{ bp() }}/admin/actions/profile"`):显示名、邮箱、日历颜色、
    「我的时区」下拉(空值 = 跟随应用默认时区,候选来自 `timezone_groups`,
    当前值不在常用清单时补一个 selected 选项)
  - **邮箱下方的验证状态块**(2026-10-12 新增,`templates/partials/me.html:35`–`:45`):
    - `verify_state.needs_verification` 为真 → 红色提示「邮箱 `{{ profile.email }}` 还没有通过验证。
      未验证期间:不接收日历邮件,也不会收到自己的订阅地址邮件。」+
      `<a href="{{ bp() }}/verify-email">去验证 →</a>`;
    - 否则 `verify_state.verified_at` 非空 → 灰字「邮箱已验证(本地时间)」;
    - 两者都不成立(没有邮箱)→ 不渲染任何提示,只留上面那条邮箱说明。
  - 邮箱说明文案已写明「**新填或修改邮箱都需要重新验证一次**」
  - **修改口令**表单(`hx-post="{{ bp() }}/admin/actions/password"`):当前口令 / 新口令 / 确认新口令
  - **我的订阅地址**:`{% include "partials/feeds.html" %}`
- **上下文**:`profile`、`csrf_token`、`current_user`、`feed_tokens`、`new_token`、
  `subscription_prefix`、**`fixed_feed_url`**、**`fixed_token_id`**、**`verify_state`**、
  `timezone_groups`、`timezone_known`、`current_timezone`、`default_timezone`、
  `notice`、`error`
- **不变量**:邮箱提示里写明「填了就会收到你自己可见日历里尚未结束的日程快照;留空即停止收信」
  ——与 `app/accounts.py::sync_user_recipient` 的实际行为一致
  (**且该账号的邮箱已验证**);验证状态只由 `verify_state` 决定,模板不自己比较时间
- **整页与片段必须喂同一套变量**:整页 `me.html` 就是 `{% include "partials/me.html" %}`,
  `app/routers/pages.py::me_page` 与 `::_me_partial` 都经
  `_me_context(session, settings, current)`(`app/routers/pages.py:391`)取同一份上下文,
  所以固定地址卡片、验证状态块在**整页与片段里都会渲染**。
  这条曾经是坑:整页少喂 `feed_tokens` / `fixed_feed_url` / `fixed_token_id` / `verify_state`,
  Jinja 默认的 `Undefined` 会把它们安静地渲染成空串(页面 200,但订阅地址是空的、看不到验证状态),
  现在的兜底是 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined`
  ——它把 `templates.env.undefined` 换成 `StrictUndefined`,缺键当场报错
- **测试**:`tests/test_family_pages.py::test_profile_update_starts_email_verification`
  (旧名 `test_profile_update_creates_mail_recipient` 已不存在;该用例断言保存后页面里出现验证提示)、
  `::test_profile_rejects_invalid_timezone`、`::test_member_partials_render`(片段)、
  `::test_all_pages_render_with_strict_undefined`(整页 + 全部管理页在 `StrictUndefined` 下渲染)

## `templates/partials/members.html`(2026-10-08 新增)

- **用途**:`/admin/members` 的面板,目标 `#members`,三段
- **结构**:
  - **邀请码**:`new_invite_url` 非空时给出**完整邀请链接**
    (`<input readonly value="{{ new_invite_url }}">`,由 `urls.invite_url()` 拼好,
    含公开基地址与 URL 编码后的明文,`templates/partials/members.html:19`–`:23`);
    卡片上方提示「链接里已带公开地址 `{{ invite_base_url }}…`」(`:13`);
    新建表单为 `label` / `max_uses`(默认 1)/ `valid_days`(0 = 不过期);
    列表列是 名称 / 已用次数(`used_count`/`max_uses`)/ 有效期 / 状态(已停用 / 已用完 /
    可用(剩 N 次))/ 操作——**没有指纹列**,邀请码明文与哈希都不在列表里
  - **账号**:`reset_username` + `new_password` 非空时给一次性临时口令;
    每行有停用/启用、重置口令、角色下拉(管理员/成员)、删除四个表单
  - **共享日历与权限**:每个共享日历一张表,每行一个账号 + 权限下拉
    (可管理 / 可编辑 / 只读),底部是新建共享日历的表单(名称 + 颜色)
- **上下文**:`members`、`invites`、`shared_calendars`、`member_role_labels`、`role_labels`、
  `current_user`、`csrf_token`、`new_invite`、**`new_invite_url`**、**`invite_base_url`**、
  **`public_base_url`**、`new_password`、`reset_username`、
  `default_timezone`、`notice`、`error`
  (`new_invite` 这个明文键仍由 `_members_partial` 传入,但**模板只用 `new_invite_url`**)
- **不变量**:所有破坏性按钮的 `hx-confirm` 都要说清后果
  (删账号:「他的个人日历与订阅地址会一并删除,共享日历里的历史日程保留」);
  邀请明文只在创建响应里出现一次,列表只给状态与次数,
  需要地址时再次提交只能一起重建(提示文案已写明「离开本页后无法再查看明文」)
- **测试**:`tests/test_family_pages.py::test_members_page_lists_all_accounts`、
  `::test_admin_creates_shared_calendar_with_viewer_defaults`、
  `::test_invite_plaintext_is_shown_once_and_register_link_works`、
  `::test_reset_member_password_shows_one_time_password_once`、
  `tests/test_public_prefix.py::test_invite_link_contains_public_base_url`
  (链接里是 `https://example.test/calendar/register?code=…`,不是裸 `/register`)

## `templates/partials/recipients.html`

- **变更(2026-10-08)**:
  - 标题语义从「同步人员」改为「邮件收件人」,提示改为「账号邮箱在『我的设置 → 个人资料』
    里由成员自己维护,这里主要管理**外部邮箱**」
  - 新增表单增加必填的 `acknowledge` 复选框(「我确认:外部邮箱只会收到家庭共享日历
    (不含任何人的私人日程)」)
  - 表格新增「范围」列
  - 重新启用已停用的行时,表单自动带上 `<input type="hidden" name="acknowledge" value="1">`
- **变更(2026-10-11)**:「范围」列改为**三种**显示(`templates/partials/recipients.html:44`–`:57`),
  先用 `{% set scope_id = recipient.scope_user_id or recipient.user_id %}` 统一范围后分支:
  - `recipient.user_id` 非空 → `本人可见日历` 胶囊,再从 `users` 里
    `selectattr('id','equalto',recipient.user_id)` 找到并显示用户名;
  - 否则 `scope_id` 非空 → `沿用{用户名}的可见日历` 胶囊 + `历史绑定` 灰字
    (用户名查不到时退回「成员」)——这是单用户版升级上来的历史外部邮箱;
  - 否则 → `家庭共享日历` 胶囊。
- **上下文**:`recipients`、`users`、`csrf_token`、`default_timezone`、
  `mail_configured`、`notice`、`error`
- **变更(2026-10-12 第四轮:设置里的同步邮箱收敛到外部邮箱)**:
  - `recipients` 只包含**外部收件人**(`user_id IS NULL`),上面那张可操作的表格
    只剩外部邮箱;账号邮箱不再出现 toggle/delete 表单;
  - 上下文**新增 `account_recipients`**(`[{user, email, verified, deliverable, status}]`):
    表格下方多一段只读的「账号邮箱(自动同步,不能在这里增删)」,
    列为「成员 / 邮箱 / 收信范围 / 状态」,状态文案来自
    `app/routers/pages.py::_account_recipient_entries`
    (`自动同步中` / `邮箱待验证` / `未填邮箱` / `账号已停用,不收信`),
    `deliverable` 为假时整行套 `muted-row`;
  - 「范围」列的三种文案现在只会落到**历史绑定的外部邮箱**上
    (`scope_user_id` 非空)→ `沿用{用户名}的可见日历` + `历史绑定`;
  - 顶部提示改写为「这里只管理**外部邮箱**……账号邮箱通过验证后自动成为收件人,
    要停止某个账号收信请到成员页停用该账号」
  - 空态文案改为「还没有外部收件人。要给没有账号的家人发日历,在上面填一个邮箱即可。」
- **测试**:`tests/test_family_pages.py::test_recipient_partial_shows_scope`
  (只断言 `本人可见日历` 与 `家庭共享日历` 两种文案);
  `tests/test_account_recipient_sync.py::test_recipients_partial_lists_account_mail_as_read_only`
  (账号邮箱在只读列表里可见、`自动同步中` 出现、页面里**没有**该行的 toggle/delete 地址)

## `templates/settings.html`

- **变更(2026-10-08)**:
  - 「同步人员」卡片改名为「邮件收件人」,提示写明「账号邮箱只收到自己可见且尚未结束的日程;
    外部邮箱只收家庭共享日历范围」
  - 新增「家庭订阅地址」卡片,`{% include "partials/family_feeds.html" %}`
  - 「远程接入令牌」卡片提示补充「每枚令牌绑定一个账号与可写日历」
- **上下文**:`overrides`、`recipients`、`account_recipients`、`tokens`、
  `family_feed_tokens`、`users`、`shared_calendars`、`default_owner`、`smtp`、时区候选
  (见 `docs/static/app-routers-pages.md`)
- **变更(2026-10-12 第四轮)**:「邮件收件人」卡片右上角胶囊由「按范围分别发送」改为
  「这里只加外部邮箱」,提示改写为「**账号邮箱是自动的**……这张卡片管理的是**外部邮箱**
  ——只收家庭共享日历范围」
- **测试**:`tests/test_agent_tokens.py::test_settings_page_carries_the_token_card`、
  `tests/test_family_pages.py::test_admin_pages_render`

## `templates/login.html` / `templates/register.html` / `templates/verify_email.html`

- **login.html 变更**:副标题由「仅允许本机访问的管理入口」改为「家庭成员的登录入口」;
  新增 `notice` 提示区;底部提示改为「忘记口令请联系家庭管理员重置;还没有账号?
  请向管理员索取邀请码」,并新增 `<a href="{{ bp() }}/register">去注册</a>`
- **register.html(2026-10-08 新增)**:
  - 邀请码注册表单:邀请码 / 用户名 / 显示名(可选)/ **邮箱(必填)** / 口令 / 确认口令,
    提交按钮文案「注册并发送验证码」
  - **邮箱字段带 `required` 与服务端 422 双重把关**(`<span class="muted">必填 · 用于收验证码</span>`);
    底部提示写明「注册后需要到『邮箱验证』页填入收到的 6 位验证码:验证通过后账号才能使用,
    系统随后会把**属于你的固定订阅地址**发到该邮箱」
  - CSRF 用的是**登录专用**的 `login_csrf`(不是 `csrf_token`),并额外输出隐藏的 `cookie_value`
  - 已登录用户访问会被重定向(见 `docs/static/app-routers-auth.md`)
- **register.html 上下文**:`error`、`code`、`username`、`display_name`、`email`、
  `login_csrf`、`cookie_value`、`calendar_name`
  (**没有 `notice`** —— 注册页只显示错误,文案来自 `app/routers/auth.py::_register_context`)
- **verify_email.html(2026-10-12 新增)**:整页(自带完整 HTML shell,**不** `extends base.html`),
  四个表单都提交到 `{{ bp() }}/...`:
  1. 验证码表单 → `POST /verify-email`:数字键盘 `inputmode="numeric"`、
     `autocomplete="one-time-code"`、`pattern="[0-9]*"`、`maxlength="{{ code_length }}"`;
  2. 重发表单 → `POST /verify-email/resend`:`resend_wait > 0` 时按钮 `disabled`
     并显示「重新发送(N 秒后)」,否则「重新发送验证码」;
  3. 改地址表单 → `POST /verify-email/email`:预填 `email`,`required`;
  4. 退出表单 → `POST /logout`。
  正文用 `code_length` / `ttl_minutes` / `max_attempts` 三个变量说明验证码规则
  (6 位、30 分钟内有效、最多可试 5 次),并写明「验证通过后账号才能使用」
- **verify_email.html 上下文**(`app/routers/auth.py::_verify_context`,`:430`):
  `current_user`、`csrf_token`、`email`、`resend_wait`、`code_length`、`ttl_minutes`、
  `max_attempts`、`notice`、`error`、`calendar_name`。
  注意它用的是**普通 `csrf_token`**(不是注册页那个 `login_csrf`);
  `notice`/`error` 由 `?sent=1` / `?sent=0` 决定(发信失败时给 error 文案)
- **不变量**:验证页**只有**「验证码 / 重发 / 改地址 / 退出」四个出口,
  绝不出现跳过验证或进入管理面的链接;四个表单都必须带 CSRF;
  第四个表单的 action 是 `bp()/logout`(不是登录页地址)
- **测试**:`tests/test_family_web.py::test_register_page_and_submit`、
  `::test_register_rejects_bad_invite_code`、`::test_register_rejects_password_mismatch`、
  `::test_logged_in_user_is_redirected_away_from_register`、
  `::test_register_page_gets_csp`(注册页也要过严格 CSP);
  `tests/test_email_verify.py::test_unverified_member_is_blocked`(被闸门送到该页)、
  `::test_resend_cooldown_then_new_code`(按钮的 `resend_wait` 禁用态)、
  `::test_can_fix_a_mistyped_email_from_the_verify_page`(改地址表单)

## `templates/bootstrap.html`(2026-10-14 新增)

- **用途**:首位管理员的「设置管理员口令」页。只在初始口令救援通道开着
  (`app/bootstrap_access.py::pending_owner` 非空)时由 `GET /bootstrap` 渲染;
  口令一设置,通道关闭,这个地址立刻 404(见
  [`app-routers-auth.md`](app-routers-auth.md)、[`app-bootstrap-access.md`](app-bootstrap-access.md))
- **结构**:整页**自带完整 HTML shell**(**不** `extends base.html`,与
  `login.html` / `register.html` / `verify_email.html` 同款),`<body class="login-body">`
  里一个 `<form class="login-card" method="post" action="{{ bp() }}/bootstrap">`(`:10`):
  - **隐藏字段**(`:20`):`<input type="hidden" name="csrf_token" value="{{ login_csrf }}">`
    ——用的是**登录专用** CSRF(与登录/注册页同一套「双提交 + 服务端签名」,
    不是 `base.html` 的 `csrf_token`),服务端由
    `app/routers/auth.py::bootstrap_submit` 的 `verify_login_csrf` 校验;
  - **字段三个**(顺序即页面顺序):`code`(`:23`,`type="text"`、`autocomplete="off"`、
    `autofocus`、`required`,**预填 URL 上的 `?code=`**)、
    `password`(`:31`,`type="password"`、`autocomplete="new-password"`、`required`)、
    `password_confirm`(`:34`,同上);
  - **提示区**(`:17`–`:18`):`notice` / `error` 两个 `alert` 块(401/422 的错误文案就渲染在这里);
  - **引导码文件路径**(`:26`–`:28`):正文给出 `{{ code_file }}`,
    以及容器里的读法 `docker compose exec calendar cat {{ code_file }}`
    ——**只给路径,不显示码本身**(码要靠管理员自己去文件里读,或从地址栏带进来);
  - **按钮与结尾提示**(`:37`–`:41`):「设置口令并关闭引导通道」;底部写明
    「设置成功后引导码文件会被删除,这个地址也随之失效」,并给出
    `python -m app.cli reset-admin-password` 作为忘记口令时的兜底
  - 样式引用 `{{ asset('css/app.css') }}`(`:7`,带内容指纹,同 2026-10-12 第三轮的约定)
- **上下文**(`app/routers/auth.py::_bootstrap_context`,`:621`):`request`、`error`、
  `notice`、`code`、`login_csrf`、`calendar_name`、`code_file`、`cookie_name`、
  `cookie_value`。**注意**:与 `register.html` 不同,本模板**没有渲染 `cookie_value`**
  ——`cookie_value` 仍被喂进来(与 `_login_context` 保持一致),但页面只输出
  `csrf_token` 这一个隐藏字段,双击校验靠 `GET /bootstrap` 写下的
  `sisi_login_csrf` Cookie(`app/routers/auth.py:658`)
- **不变量**:
  - 表单必须 POST 到 **`{{ bp() }}/bootstrap`**(跟随当次请求前缀,带前缀入口下就是
    `/calendar/bootstrap`),不得写死裸路径;
  - **不使用内联脚本**:模板里没有 `<script>`、也没有事件属性;严格 CSP 由
    `app/urls.py::is_admin_path` 的集合含 `/bootstrap` 保证(见
    [`app-urls.md`](app-urls.md));
  - 引导码明文在页面里**只能**作为 `code` 输入框的预填值出现,不得写进
    `data-*`、注释或任何 JS 可见的位置;模板不得自己去读文件;
  - 页面上**不得**出现任何「跳过引导、直接进管理面」的链接(它只有一个出口:
    设置成功后的 303 到 `/login`)
- **测试**:`tests/test_bootstrap_access.py::test_bootstrap_page_serves_the_form_with_csp`
  (200 + `Content-Security-Policy` 头 + URL 上的码出现在页面里 + 带前缀入口同样 200)、
  `::test_setting_the_password_closes_the_channel`(用页面里的 `csrf_token` 提交成功)、
  `::test_bootstrap_rejects_a_missing_csrf_token`(表单的 CSRF 是真校验,不是装饰)
- **未覆盖**:「模板里没有内联脚本」这一条**没有**专门断言——
  `tests/test_family_pages.py::_assert_no_inline_script` 只遍历成员/管理页与片段,
  不包含 `/bootstrap`;引导页也没有进入 `tests/test_page_security.py::CSP_PAGES`。

## `templates/partials/settings_form.html`

- **变更**:「默认时区」由 `<input list="tz-list-settings">` 改为按地区分组的 `<select name="default_timezone">`
  - 选项文本:`{{ item.index }}. {{ item.label }}({{ item.iana }})`,如 `1. 北京时间(Asia/Shanghai)`
  - `value` 仍是 IANA 名,保证已有表单/接口调用不变
  - 当 `current_timezone` 不在 `timezone_known` 中时,补一个
    `{{ current_timezone }}(当前值,不在常用清单中)` 且 `selected`,不会静默改掉用户配置
  - 下拉框下方 `<p class="hint">` 说明序号可被 `event` 接口直接使用
- **移除**:`<datalist id="tz-list-settings">`(不再有输入框引用它)
- **上下文**:`timezone_groups`、`timezone_known`、`current_timezone`、`overrides`、`settings`
- **测试**:`test_settings_page_renders_timezone_dropdown`、`test_settings_page_keeps_custom_timezone`

## `static/js/copy.js`(2026-10-12 第二轮新增)

- **作用**:通用「一键复制」——任何带 `data-copy="<选择器>"` 或
  `data-copy-text="<字面量>"` 的元素被点击时复制对应内容
- **为什么这么做**:管理页 CSP 是 `script-src 'self'`,内联 `onclick` 会被浏览器拦掉
  (项目约定见 `AGENTS.md`),所以用**事件委托**挂在 `document` 上:
  HTMX 换掉片段(`#feeds`、`#family-feeds`、`#members`)之后不需要重新绑定
- **行为**:
  - 优先 `navigator.clipboard.writeText()`(安全上下文;tailnet 是 https、
    `localhost` 也算安全上下文);失败或不可用时退回
    「临时 textarea + `document.execCommand('copy')`」
  - 复制成功按钮文字临时变「已复制」(加 `.copy-ok`),失败变「复制失败」(`.copy-fail`),
    1.6 秒后恢复原文;原文存在 `dataset.copyOriginal` 里
  - 点击只读输入框(`readOnly`)会自动全选,方便手动 Ctrl+C
- **加载**:`templates/base.html` 用 `asset('js/copy.js')` + `defer`
- **不变量**:模板里**不得**出现内联事件属性;
  复制目标用 `id` 选择器,`id` 必须在同一文档里唯一
  (`#fixed-feed-url` / `#new-feed-url` / `#new-family-feed-url` / `#new-invite-url`)
- **测试**:`tests/test_family_pages.py::test_static_assets_carry_a_version_stamp`
  (抓到该文件 200、含 `navigator.clipboard` 与 `execCommand` 兜底)、
  `::test_feed_cards_offer_a_copy_button`、`::test_new_token_reveals_offer_a_copy_button`;
  页面级的内联处理器由 `tests/test_family_pages.py` 的 `_assert_no_inline_script`
  与 `tests/test_page_security.py` 兜底
- **未覆盖**:真实点击后的剪贴板内容没有自动化断言(需要浏览器驱动),只做了结构断言

## 前端协作:公开前缀与 `data-base-path`(2026-10-12)

- **传值方式**:`templates/base.html:11` 输出 `<body data-base-path="{{ bp() }}">`,
  这是模板把「当次请求的前缀」交给 JS 的**唯一**通道(严格 CSP 不允许内联脚本)
- **JS 读法**(两个文件都是同一行):
  - `static/js/calendar.js:11`:`const basePath = (document.body && document.body.dataset.basePath) || "";`
  - `static/js/event_form.js:11`:同上
- **怎么用**:
  - 接口地址:`form.dataset.api || basePath + "/api/v1"`、`host.dataset.api || basePath + "/api/v1"`
  - 跳转:`basePath + "/admin"`、`basePath + "/login?next=" + encodeURIComponent(basePath + "/admin")`、
    `basePath + "/admin/events/new?" + params`、`basePath + "/admin/events/" + event_id`
- **不变量**:JS 里**不得**写死 `/api`、`/admin`、`/login`、`/static`;
  需要别的前缀时用元素的 `data-api` / `data-redirect` 覆盖;
  `basePath` 为空串就是本机裸路径,这两个分支都必须能用
- **测试**:`tests/test_public_prefix.py::test_prefixed_json_api_reports_401_in_json`、
  `::test_admin_pages_under_prefix_link_within_the_prefix`、
  `::test_base_url_without_path_falls_back_to_bare_paths`

## 测试缺口(与本轮变更相关)

- **整页 `/admin/me` 的上下文缺口已被修掉、但只有弱断言**:整页与片段现在共用
  `_me_context`(`app/routers/pages.py:391`),不再出现「订阅地址空白、验证状态看不见」;
  兜底用例 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined`
  只保证「不缺键」,没有断言整页里真的渲染出固定地址输入框与验证提示。
- **`/verify-email` 没有 CSP 用例**:`tests/test_page_security.py::CSP_PAGES` 只有
  `/admin`、`/admin/settings`、`/admin/sync`、`/admin/events/new`,
  注册页由 `tests/test_family_web.py::test_register_page_gets_csp` 单独覆盖,
  **验证页没有对应的兜底断言**(它同样自带完整 HTML shell);
  渲染层面的兜底倒是有:`tests/test_email_verify.py::test_verify_page_renders_with_strict_undefined`
  (把 `templates.env.undefined` 换成 `StrictUndefined` 后仍能渲染)。
- **整页缺上下文只知道「不缺键」**:`tests/test_family_pages.py::test_all_pages_render_with_strict_undefined`
  保证整页不再把变量渲染成空串,但**没有**断言整页里真的出现固定地址输入框或验证提示文案。
- **固定地址行「界面不渲染停用/撤销按钮」没有断言**:现有用例只断言直接 POST 返回 409。
- **页脚 `public_base_url` 没有直接断言**:`test_public_prefix.py::test_subscription_url_comes_from_public_base_url`
  覆盖的是订阅地址来源,不是 `base.html` 页脚那一行。
  (2026-10-12 第二轮已补 `tests/test_family_pages.py::test_footer_and_sync_partial_show_the_public_base_url`,
  它断言页脚出现 `<基地址>/login`、同步面板出现「公开面基地址」。)
- **`/bootstrap` 没有进 CSP 兜底清单**(2026-10-14):`tests/test_page_security.py::CSP_PAGES`
  与 `tests/test_family_pages.py::_assert_no_inline_script` 都不包含它,
  严格 CSP 只有 `tests/test_bootstrap_access.py::test_bootstrap_page_serves_the_form_with_csp`
  一条断言(只查响应头,不查页面里有没有内联脚本);引导页同样自带完整 HTML shell。

## 变更记录

- 2026-09-29:设置页改下拉框;日程表单补序号提示与候选标签;邮件队列新增操作/发送时间。
- 2026-09-29:新增 `partials/tokens.html`;设置页新增远程接入令牌卡片。
- 2026-09-30:日程表单新增提醒勾选组、自定义分钟与链接输入框。
- 2026-10-06:`partials/sync_status.html` 的「发送最新版」二次确认文案改为尚未结束的日程快照。
- 2026-10-08:家庭版模板清单与角色分级导航;新增 `me/members/register` 整页与
  `partials/me|feeds|family_feeds|members` 片段;`calendar.html` 多日历侧栏与
  `data-calendars`、`event_form.html` 日历下拉与只读态、`recipients.html` 范围列;
  全部模板重申无内联脚本、值经 `data-*` 传递。
- 2026-10-11:`partials/recipients.html` 范围列扩为三种显示(本人可见日历 /
  沿用某成员的可见日历 + 历史绑定 / 家庭共享日历),片段上下文未变;
  模板清单里该行的说明同步更新。
- 2026-10-12:公开前缀与邮箱验证。所有模板的站内链接改走 `bp()`,
  `base.html` 输出 `<body data-base-path>`、页脚第二行改用 `public_base_url`;
  新增整页 `verify_email.html`(四个表单:验证码 / 重发 / 改地址 / 退出)并补进模板清单;
  `register.html` 写明邮箱必填、按钮改为「注册并发送验证码」,并订正其上下文
  (**没有 `notice`**,CSRF 键是 `login_csrf` 且多一个 `cookie_value`);
  `partials/feeds.html` 增加固定地址卡片与「固定」chip(操作列改成一句提示,不渲染停用/撤销);
  `partials/me.html` 增加验证状态块;`partials/members.html` 的邀请链接改用
  `new_invite_url`;`partials/sync_status.html` 的公开面一行改用 `sync_state.public_base_url`
- 2026-10-12(第四轮):`partials/recipients.html` 拆成「外部邮箱(可维护)」与
  「账号邮箱(只读,自动同步)」两段,上下文新增 `account_recipients`;
  `settings.html` 的收件人卡片文案与胶囊改写;`partials/sync_status.html` 的
  「邮件收件人」一行补注「账号邮箱自动 + 外部邮箱」。
  测试:`tests/test_account_recipient_sync.py::test_recipients_partial_lists_account_mail_as_read_only`。
  (**模板里已无 `subscription_url`**);新增「前端协作:公开前缀与 `data-base-path`」与
  「测试缺口」两节。测试名同步为
  `test_my_feed_is_stable_and_temporary_token_shown_once` 与
  `test_profile_update_starts_email_verification`(旧名已不存在)。
- 2026-10-12(同日后补):整页 `/admin/me` 的上下文补齐 —— 模板侧无改动,但
  `app/routers/pages.py::_me_context` 成为整页与片段的共用上下文,`new_feed_token`
  这个上下文键不再存在(统一叫 `new_token`),所以 `partials/me.html` 的上下文列表同步更正;
  新增 `tests/test_family_pages.py::test_all_pages_render_with_strict_undefined` 与
  `tests/test_email_verify.py::test_verify_page_renders_with_strict_undefined`
  两条 `StrictUndefined` 兜底用例。
- 2026-10-12(第三轮:真机 UI 反馈):
  - **静态资源加指纹**:`base.html` / `calendar.html` / `event_form.html` /
    `login.html` / `register.html` / `verify_email.html` 全部改用 `asset(...)`,
    模板里不再有 `{{ bp() }}/static/...`(真机上「侧栏错版」的根因就是浏览器缓存了旧 CSS)
  - **侧栏排版重做**(真机反馈「新建日程那块太丑」):`.sidebar-cta` + 内联 SVG「+」图标、
    `.sidebar-label` 小标题、带边框圆角的 `.calendar-list`(行间细线)、提示文案缩短
  - **订阅地址一键复制**:`partials/feeds.html`(固定地址 + 临时明文)、
    `partials/family_feeds.html`(临时明文)、`partials/members.html`(邀请链接)
    各加 `data-copy` 按钮;新增 `static/js/copy.js`(事件委托 + 剪贴板 API +
    `execCommand` 兜底 + 点只读框全选)
  - 表格样式:`.table th { white-space: nowrap }`、`.table td.row-actions { white-space: nowrap }`,
    并把固定行的说明从「固定地址不提供停用或撤销」缩短为「不可撤销」(完整理由进 `title`)
  - 测试:`tests/test_family_pages.py::test_sidebar_primary_action_is_a_proper_button`、
    `::test_static_assets_carry_a_version_stamp`、`::test_feed_cards_offer_a_copy_button`、
    `::test_new_token_reveals_offer_a_copy_button`
- 2026-10-14:首位管理员引导码。新增整页 `templates/bootstrap.html`(设置管理员口令:
  隐藏 `csrf_token` = 登录 CSRF、三个字段 `code` / `password` / `password_confirm`、
  只展示引导码**文件路径**、按钮「设置口令并关闭引导通道」),并补进模板清单;
  站内链接走 `bp()`、样式走 `asset()`、**无内联脚本**(严格 CSP 由
  `app/urls.py::is_admin_path` 新增的 `/bootstrap` 保证);
  写明它与 `register.html` 的差异(**没有渲染 `cookie_value`**);
  「测试缺口」补一条:引导页没有进 CSP 兜底清单。
