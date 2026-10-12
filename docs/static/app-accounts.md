# `app/accounts.py`

本模块是「谁看得见什么、谁改得了什么」的唯一判定处:账号 CRUD 与认证、私人/共享日历、
共享日历成员角色、`CalendarAccess` 可见可写判定,以及账号收件人(身份 `user_id` /
范围 `scope_user_id`,见 `docs/static/migrations-0006.md`)与展示时区的解析。
路由与导出都必须经过它,不允许各自拼 SQL 判权限。

本轮(2026-10-12)另加两件事:`create_user` 可以顺手登记账号的**固定订阅地址**
(`settings=...`),以及账号收件人的前置条件收紧为「**有邮箱且已验证**」
(`app/email_verify.py::is_verified`)。

**隐私基调(必须保持)**:私人日历(`calendars.kind='personal'`)只有归属账号可读可写,
**管理员也不能读别人的私人日历**——读一律 404;写也写不了(`can_write` 为 `False`,
在页面上通常先撞到可见性判定而返回 404,而对**可见但只读**的共享日历写操作返回 403)。
管理员管的是账号、邀请码与全局设置,不是别人的私人日程。
共享日历写入仅限 `owner`/`editor`,新成员默认 `viewer`。

错误约定:字段级校验失败抛 `AccountError`(带 `fields` 字典,路由转成 422 结构化错误);
注册流程里邀请码不可用抛 `ValueError`,文案可直接展示给用户。

## 新增

### `AccountError`

- **位置**:`app/accounts.py:64`
- **用途**:字段级校验失败;`fields` 是「字段名 → 中文提示」字典
- **返回**:异常;`str(exc)` 把字段拼成 `key: value; ...` 便于日志
- **副作用**:无
- **不变量**:路由只允许把 `fields` 映射成 422 响应体,不要把内部细节透出
- **测试**:`tests/test_family_accounts.py::test_membership_validation`(非法成员角色)、
  `::test_user_timezone_falls_back_to_default`(非法时区不写入)

### `RegistrationError`

- **位置**:`app/accounts.py:72`
- **用途**:注册类失败的语义标记(邀请码无效、用户名被占用等)
- **说明**:当前实现由 `app/invites.py` 直接抛 `ValueError` 传递文案,本类保留为语义占位
- **测试**:暂无直接测试

### `validate_username(raw) -> str`

- **位置**:`app/accounts.py:79`
- **用途**:账号名校验:先走 `app/models.py::normalize_username`(去空白 + 小写),再校验长度与字符集
- **参数**:上限 `MAX_USERNAME = 64`(`app/accounts.py:50`);必须匹配
  `USERNAME_PATTERN = ^[a-z0-9][a-z0-9._-]*$`(`app/accounts.py:56`),
  即只能用小写字母、数字、点、下划线、连字符,且以字母或数字开头
- **返回**:规范化后的账号名
- **副作用**:无
- **不变量**:账号名入库前必须小写,否则会出现 `Mama`/`mama` 两个账号
- **测试**:`tests/test_family_accounts.py::test_duplicate_username_is_rejected`(经 `create_user`)

### `validate_password(raw, *, field="password") -> str`

- **位置**:`app/accounts.py:92`
- **用途**:口令校验:长度在 `MIN_PASSWORD_LENGTH = 8` 与 `MAX_PASSWORD_LENGTH = 200` 之间,
  且首尾无空白
- **参数**:`field` 决定错误挂在哪个表单字段上(注册/改密用不同字段名)
- **返回**:原样口令(不做强度评分)
- **副作用**:无
- **不变量**:口令明文只在本函数与 `hash_password` 之间短暂存在,不得写日志
- **测试**:`tests/test_family_web.py::test_register_rejects_password_mismatch`(经注册路由的字段校验);
  长度边界**暂无直接测试**

### `normalize_optional_email(raw) -> str | None`

- **位置**:`app/accounts.py:103`
- **用途**:邮箱可选:空值返回 `None`,给了就必须通过 `EMAIL_PATTERN` 且 ≤320 字符
- **返回**:小写去空白的邮箱,或 `None`
- **副作用**:无
- **不变量**:入库邮箱一律小写;**唯一性是应用层约束**(`email_taken`),数据库上只有普通索引
- **测试**:`tests/test_family_accounts.py::test_email_must_be_unique`、
  `tests/test_family_pages.py::test_profile_cannot_steal_another_accounts_email`

### `validate_display_name(raw) -> str | None`

- **位置**:`app/accounts.py:113`
- **用途**:显示名可选:空值返回 `None`,上限 `MAX_DISPLAY_NAME = 100`
- **返回**:去首尾空白后的显示名,或 `None`
- **副作用**:无
- **不变量**:显示名只影响展示(`User.label` 与私人日历名),不参与匹配或鉴权
- **测试**:暂无直接测试

### `get_user(session, user_id) -> User | None`

- **位置**:`app/accounts.py:125`
- **用途**:按 id 取账号
- **参数**:`user_id` 为空/`None` 时直接返回 `None`,不查库
- **返回**:`User` 或 `None`(不存在即 `None`,不抛错)
- **副作用**:无
- **测试**:经 `tests/test_family_mail.py::test_resolve_targets_covers_each_enabled_user_and_family` 间接覆盖

### `get_user_by_username(session, username) -> User | None`

- **位置**:`app/accounts.py:131`
- **用途**:按规范化后的账号名取账号
- **返回**:`User` 或 `None`(空名直接 `None`)
- **副作用**:无
- **不变量**:查询前必须走 `normalize_username`,保持大小写不敏感
- **测试**:`tests/test_family_accounts.py::test_delete_user_removes_personal_calendar_but_keeps_shared_events`、
  `tests/test_family_web.py::test_register_page_and_submit`

### `list_users(session) -> list[User]`

- **位置**:`app/accounts.py:140`
- **用途**:列出全部账号,供成员页与导出目标使用
- **返回**:按 `created_at.asc(), id.asc()` 排序,同一秒创建也能稳定定序
- **副作用**:无
- **不变量**:顺序即「创建先后」,**首位管理员 = 其中第一个 `admin`**(见 `owner_user`)
- **测试**:`tests/test_family_pages.py::test_members_page_lists_all_accounts`、
  `tests/test_family_mail.py::test_resolve_targets_covers_each_enabled_user_and_family`

### `list_members(session) -> list[User]`

- **位置**:`app/accounts.py:149`
- **用途**:只列普通成员(排除 `admin`),供成员管理页使用
- **返回**:`list_users` 过滤掉管理员后的列表
- **副作用**:无
- **测试**:暂无直接测试(当前页面走 `list_users` 全量渲染)

### `owner_user(session) -> User | None`

- **位置**:`app/accounts.py:154`
- **用途**:取**首位管理员**——`.env` 时代的 Agent 令牌、历史数据归属、以及每个账号的
  **固定订阅地址**(派生自账号 id)的兜底解释都落在他身上
- **返回**:第一个 `role == 'admin'` 的账号;没有管理员时退化为第一个账号;没有任何账号时 `None`
- **副作用**:无
- **不变量**:判定顺序依赖 `list_users` 的稳定排序;**不得**改成「任意管理员」,
  否则升级归属会在多管理员时漂移。
  **旧的无令牌订阅地址 `/calendar`、`/calendar.ics` 已经取消**,本函数不再与任何对外
  订阅地址绑定(唯一例外是「历史无归属 Agent 令牌兜底绑首位管理员」,见
  `docs/static/app-deps.md`)
- **测试**:`tests/test_family_accounts.py::test_owner_is_first_admin`、
  `::test_bootstrap_is_idempotent`、`tests/test_family_web.py::test_owner_fixed_address_serves_owner_scope_not_union`
  (首位管理员的**固定**地址只服务他自己的可见范围,与「旧地址」无关)

### `next_color(session) -> str`

- **位置**:`app/accounts.py:163`
- **用途**:按现有账号数取下一个默认颜色
- **返回**:`DEFAULT_USER_COLORS[len(list_users) % 6]`
- **副作用**:无
- **不变量**:只是默认值,调用方显式传 `color` 时必须优先(见 `create_user`)
- **测试**:暂无直接测试

### `create_user(session, *, username, password, display_name=None, email=None, role=ROLE_MEMBER, color=None, timezone_name=None, must_change_password=False, settings=None) -> User`

- **位置**:`app/accounts.py:169`
- **用途**:创建账号,并**自动开通**:建私人日历、加入所有共享日历(viewer)、按邮箱建账号收件人、
  **登记该账号的固定订阅地址**
- **参数**:
  - `role` 只接受 `admin`/`member`;`timezone_name` 不做校验(由 `update_profile` 负责);
    其余字段走本模块的四个校验函数
  - **`settings`(2026-10-12 新增)**:`Settings | None = None`。传了就在建号后顺手调
    `app/feed_tokens.py::ensure_account_feed_token`(幂等,只写 sha256)登记那条固定地址;
    传 `None` 则跳过,由启动引导 `ensure_family_setup` 兜底补上
    (引导每次启动都会检查,所以漏传不会留下没有固定地址的账号)。
    调用方(注册、页面建号、引导)**应尽量传**
- **返回**:已 `flush` 的 `User`(事务由调用方提交)
- **副作用**:写 `users`、`calendars`(私人日历)、`calendar_members`(共享日历 viewer)、
  可能写 `sync_recipients`(有邮箱时;未验证邮箱不建行,见 `sync_user_recipient`)、
  可能写 `feed_tokens`(传了 `settings` 时);写 info 日志
- **失败**:用户名/邮箱重复、字段非法一律抛 `AccountError`,错误**一次性汇总**
  (不会因为用户名错就不校验口令);此时不得留下任何半成品行
- **不变量**:账号创建即拥有私人日历与共享日历只读权限,不允许出现「有账号但没有日历」的状态;
  新账号的共享日历角色固定为 `viewer`,提权只能由管理员改成员角色;
  **固定地址的登记必须幂等**——重复 `ensure_account_feed_token` 不得多出第二行
- **测试**:`tests/test_family_accounts.py::test_new_member_gets_personal_calendar_and_viewer_role`、
  `::test_duplicate_username_is_rejected`、`::test_email_must_be_unique`、
  `::test_invite_registration_creates_member`、
  `::test_account_feed_token_is_stable_and_derived`(建号即有一条固定地址、重复确保仍是一行)、
  `::test_feed_tokens_are_listed_per_scope`(固定行永远排在列表第一位)、
  `tests/test_family_pages.py::test_admin_creates_shared_calendar_with_viewer_defaults`(经页面创建)

### `email_taken(session, email, *, exclude_user_id=None) -> bool`

- **位置**:`app/accounts.py:249`
- **用途**:邮箱占用查重(数据库的 `email` 只是普通索引,唯一性靠这里)
- **参数**:`exclude_user_id` 用于「改自己的资料」时排除自己
- **返回**:`True` 表示已被别的账号占用
- **副作用**:无
- **不变量**:任何写 `users.email` 的路径都必须先过这里(创建与改资料)
- **测试**:`tests/test_family_accounts.py::test_email_must_be_unique`、
  `tests/test_family_pages.py::test_profile_cannot_steal_another_accounts_email`

### `set_password(session, user, password, *, must_change=False) -> None`

- **位置**:`app/accounts.py:256`
- **用途**:改口令:校验后写 `password_hash`,并设置 `must_change_password`,刷新 `updated_at`
- **参数**:`must_change=True` 用于管理员重置后的临时口令,首次登录强制改密
- **返回**:`None`
- **副作用**:写库并 `flush`
- **不变量**:口令只以 Argon2id 摘要入库;明文不得进入日志或审计 detail
- **测试**:`tests/test_family_pages.py::test_reset_member_password_shows_one_time_password_once`、
  `::test_password_change_requires_current_password`(改密路径先验旧口令)

### `verify_user_password(user, password) -> bool`

- **位置**:`app/accounts.py:265`
- **用途**:校验某个账号的口令
- **返回**:`True`/`False`
- **副作用**:无
- **测试**:`tests/test_family_pages.py::test_password_change_requires_current_password`

### `authenticate_user(session, username, password) -> User | None`

- **位置**:`app/accounts.py:269`
- **用途**:登录时的账号口令校验
- **返回**:成功返回 `User`;**账号不存在、账号被停用、口令错误都返回 `None`**(调用方统一提示)
- **副作用**:无(登录时间由 `mark_login` 单独记录)
- **不变量**:账号不存在与账号被停用也各走一次口令哈希校验,**不得**提前返回,
  否则响应时间会暴露账号是否存在;停用账号必须不可登录
- **测试**:`tests/test_family_accounts.py::test_disabled_account_cannot_authenticate`

### `mark_login(session, user) -> None`

- **位置**:`app/accounts.py:283`
- **用途**:记录最近一次成功登录时间
- **副作用**:写 `last_login_at` 并 `flush`
- **不变量**:只在认证成功后调用
- **测试**:暂无直接测试(登录路由 `app/routers/auth.py:183` 调用)

### `set_user_enabled(session, user, enabled) -> None`

- **位置**:`app/accounts.py:288`
- **用途**:启用/停用账号
- **副作用**:写 `enabled`、`updated_at`
- **不变量**:停用后该账号不能登录,并且**从导出与邮件目标里消失**(见 `docs/static/app-models.md`);
  停用不删除数据
- **测试**:`tests/test_family_accounts.py::test_disabled_account_cannot_authenticate`、
  `tests/test_family_mail.py::test_disabled_user_is_dropped_from_targets`、
  `tests/test_family_pages.py::test_admin_can_disable_and_promote_member`

### `set_user_role(session, user, role) -> None`

- **位置**:`app/accounts.py:294`
- **用途**:改账号角色(`admin`/`member`)
- **返回**:`None`;非法角色抛 `AccountError({...})`
- **副作用**:写 `role`、`updated_at`(**不**创建或删除私人日历,也不改共享日历角色)
- **不变量**:角色只决定「能进哪些管理面」,**不授予**读别人私人日历的能力
- **测试**:`tests/test_family_accounts.py::test_membership_validation`(非法角色被拒)

### `update_profile(session, user, *, display_name=None, email=None, timezone_name=None, color=None, display_name_given=True, email_given=True, timezone_given=True, color_given=True) -> None`

- **位置**:`app/accounts.py:302`
- **用途**:更新个人资料;**只处理显式提交的字段**(表单里留空即清除,未提交的字段不动)
- **参数**:`*_given` 标记该字段本次是否提交;邮箱仍走唯一性检查(排除自己);
  时区必须是合法 IANA 名(非法则整批抛错且旧值不变);颜色必须是 `#RGB`/`#RRGGBB`
- **返回**:`None`
- **副作用**:改 `users` 字段并 `flush`,随后调 `sync_user_recipient` 同步账号收件人
  (**清空邮箱会删除该账号的收件人记录,即停止收信**;新填/改成一个**未验证**的邮箱同样
  不会立刻建行,要等验证通过)
- **不变量**:邮箱与收件人一一对应,改邮箱不得留下两条记录;
  校验失败时不得部分写入(全部错误汇总后一次性抛出)。
  **本函数不碰验证状态** —— 改邮箱后「打回待验证 + 发新验证码」由调用方
  `app/routers/pages.py::save_profile_action` 负责(它先比较新旧邮箱);
  邮箱唯一性由本函数把关
- **测试**:`tests/test_family_accounts.py::test_profile_update_syncs_mail_recipient`、
  `::test_user_timezone_falls_back_to_default`、
  `tests/test_family_pages.py::test_profile_rejects_invalid_timezone`、
  `::test_profile_cannot_steal_another_accounts_email`、
  `tests/test_family_mail.py::test_clearing_email_stops_delivery`

### `delete_user(session, user) -> None`

- **位置**:`app/accounts.py:353`
- **用途**:删除账号,并**连带清理个人数据**:他的私人日历(**含其中的全部事件**)、
  他的订阅令牌、Agent 令牌与账号收件人
- **副作用**:逐类 `session.delete` 并 `flush`(事务由调用方提交);不删文件、不发信
- **不变量**:
  - **私人日历连同其中的事件一起删除**:否则会留下 `calendar_id` 为空、不属于任何人的
    孤儿事件,又会被全库并集产物带出去;
  - **共享日历里的历史事件必须保留**,只把 `created_by_user_id` 置空,避免删人抹掉家庭日程。
    模型声明的是 `ondelete="SET NULL"`;**由 0005 迁移升级上来的既有库这几个加列没有外键约束**
    (见 `docs/static/migrations-0005.md`),那里依赖数据库自动置空的行为不成立;
  - **别人挂在该账号上的收信范围必须退回家庭共享**:把
    `scope_user_id == 该账号且 user_id IS NULL` 的行改成 `scope_user_id = None`
    (`app/accounts.py:400`–`:408`)。否则这些历史外部邮箱的附件目标会指向一个**不存在的账号**,
    `app/accounts.py::target_calendar_ids` 对不存在的账号返回空列表,等于给他们发空附件。
    只处理 `user_id IS NULL` 的行——该账号自己的账号收件人(`app/accounts.py:374`–`:377`)
    上面已经删掉了(见 `AGENTS.md` 不变量 21)。
  - 调用方(页面/API)必须另外保证**不会删掉最后一个管理员**、也不允许管理员删除自己。
- **测试**:`tests/test_family_accounts.py::test_delete_user_removes_personal_calendar_but_keeps_shared_events`、
  `tests/test_family_pages.py::test_admin_cannot_disable_or_delete_self`、
  `::test_admin_deleting_member_keeps_last_admin`;
  「把 `scope_user_id` 退回 `NULL`」这一段**暂无直接测试**

### `personal_calendar(session, user) -> Calendar`

- **位置**:`app/accounts.py:418`
- **用途**:取该账号的私人日历,**不存在就建一个(幂等)**
- **返回**:`Calendar`(`kind='personal'`,`owner_user_id=user.id`,名称 `「{user.label}的日历」`,
  颜色跟随 `user.color`)
- **副作用**:缺失时写 `calendars` 并 `flush`
- **不变量**:一个账号最多一条私人日历;它是唯一「创建者完全私有」的日历,
  **不得**出现在别人的 `CalendarAccess` 里;名称随显示名变化,但 id 稳定
- **测试**:`tests/test_family_accounts.py::test_new_member_gets_personal_calendar_and_viewer_role`、
  `::test_bootstrap_creates_first_admin_and_two_calendars`

### `list_shared_calendars(session) -> list[Calendar]`

- **位置**:`app/accounts.py:442`
- **用途**:列出全部共享日历
- **返回**:按 `sort_order.asc(), created_at.asc(), id.asc()` 排序(**不按名称**,保证渲染稳定)
- **副作用**:无
- **不变量**:共享日历对全体成员可见,判据只是 `kind == 'shared'`,
  **不要**用名称或「是不是默认那条」来判断
- **测试**:`tests/test_family_accounts.py::test_bootstrap_is_idempotent`、
  `::test_shared_calendar_creator_becomes_owner_and_members_are_viewers`

### `create_shared_calendar(session, *, name, color=None, creator=None) -> Calendar`

- **位置**:`app/accounts.py:453`
- **用途**:新建共享日历,并给**当时的所有账号**写好成员角色
- **参数**:名称为空或超过 `MAX_CALENDAR_NAME = 100` 抛 `AccountError`;
  `creator` 是发起人(通常当前管理员)
- **返回**:已 `flush` 的 `Calendar`
- **副作用**:写 `calendars` 与若干 `calendar_members`
- **不变量**:角色分配规则固定——`creator` 是 `owner`,其余 `admin` 也是 `owner`,
  普通成员一律 `viewer`。默认 `viewer` 是刻意的:文档定的规则是「成员看得到家庭日历,
  但只有管理员能改」,放开某人要让管理员显式提升为 `editor`;
  之后新加入的账号由 `join_shared_calendars` 补 `viewer`
- **测试**:`tests/test_family_accounts.py::test_shared_calendar_creator_becomes_owner_and_members_are_viewers`、
  `::test_membership_validation`(空名被拒)、
  `tests/test_family_pages.py::test_admin_creates_shared_calendar_with_viewer_defaults`

### `join_shared_calendars(session, user, *, role=MEMBER_ROLE_VIEWER) -> None`

- **位置**:`app/accounts.py:492`
- **用途**:把账号加入所有共享日历;**已存在的成员关系一律不动**(不降级也不升级)
- **副作用**:只为缺失的关系写 `calendar_members`
- **不变量**:幂等;不得覆盖已有的 `editor`/`owner`
- **测试**:暂无直接测试(由 `create_user` 与 `app/bootstrap.py::ensure_family_setup` 调用,
  效果经 `tests/test_family_accounts.py::test_new_member_gets_personal_calendar_and_viewer_role` 覆盖)

### `set_membership(session, calendar, user, role) -> CalendarMember`

- **位置**:`app/accounts.py:499`
- **用途**:设置某人在某共享日历上的角色(存在即更新,不存在则新建)
- **参数**:`role` 只接受 `owner`/`editor`/`viewer`,否则抛 `AccountError`
- **返回**:`CalendarMember`
- **副作用**:写/更新 `calendar_members` 并 `flush`
- **不变量**:`(calendar_id, user_id)` 唯一,重复调用只更新一行;
  这是**提权的唯一入口**,只应由管理员在成员管理面触发
- **测试**:`tests/test_family_accounts.py::test_editor_role_can_write_shared_calendar`、
  `::test_membership_validation`、`tests/test_family_pages.py::test_member_calendar_role_change_requires_admin`

### `membership_role(session, calendar, user) -> str | None`

- **位置**:`app/accounts.py:526`
- **用途**:查某人在某日历上的角色
- **返回**:私人日历返回 `owner`(归属人)或 `None`(其他人);共享日历查成员表,
  没有成员关系返回 `None`
- **副作用**:无
- **不变量**:私人日历的角色由**归属**推导,不查 `calendar_members`;
  返回 `None` 即「看不见」
- **测试**:`tests/test_family_accounts.py::test_new_member_gets_personal_calendar_and_viewer_role`、
  `::test_bootstrap_creates_first_admin_and_two_calendars`、
  `tests/test_family_pages.py::test_member_calendar_role_change_requires_admin`

### `calendar_members_map(session, calendar) -> dict[str, str]`

- **位置**:`app/accounts.py:538`
- **用途**:取共享日历的 `user_id → role` 映射,供成员管理页渲染
- **副作用**:无
- **不变量**:只反映 `calendar_members`,不要用它去判断私人日历
- **测试**:`tests/test_family_accounts.py::test_shared_calendar_creator_becomes_owner_and_members_are_viewers`

### `CalendarAccess`(数据类)

- **位置**:`app/accounts.py:552`(`@dataclass(frozen=True)` 在 `app/accounts.py:551`)
- **用途**:一次请求内的「可见日历 + 各自角色」快照,避免权限判断反复查库
- **字段**:`user`、`calendars`(元组,顺序即展示顺序)、`roles`(日历 id → 角色)
- **属性**:
  - `by_id`(`app/accounts.py:532`):`dict[calendar_id, Calendar]`
  - `writable_calendars`(`app/accounts.py:552`):角色在 `WRITE_MEMBER_ROLES` 里的日历
  - `shared_calendars`(`app/accounts.py:560`):非私人日历
- **不变量**:快照是**只读**的;`roles` 里同时包含私人日历的 `owner`,
  这样 `can_write` 只有一处判断逻辑

### `CalendarAccess.get(calendar_id) -> Calendar | None`

- **位置**:`app/accounts.py:563`
- **用途**:在可见范围内按 id 取日历
- **参数**:`None`/空串直接返回 `None`
- **返回**:可见则返回 `Calendar`,**不可见也返回 `None`(与不存在同形,不泄露存在性)**
- **测试**:经 `app/routers/api_events.py` 与 `tests/test_family_web.py::test_admin_cannot_move_event_into_member_private_calendar` 间接覆盖

### `CalendarAccess.can_view(calendar_id) -> bool`

- **位置**:`app/accounts.py:568`
- **用途**:能否看见该日历(= 该日历在本次快照里)
- **返回**:`True`/`False`;`None` 或空串为 `False`
- **副作用**:无
- **不变量**:**所有读接口都必须经过它或 `viewable_ids()`**;越界日历在列表里不出现,
  按 id 单查返回 404(读路径对「别人的私人日历」一律 404,不返回 403,
  因为 403 会泄露「它存在」)
- **测试**:`tests/test_family_web.py::test_admin_cannot_move_event_into_member_private_calendar`、
  `::test_member_cannot_edit_other_peoples_private_event`、
  `::test_member_cannot_open_someone_elses_event_page`

### `CalendarAccess.can_write(calendar_id) -> bool`

- **位置**:`app/accounts.py:571`
- **用途**:能否写该日历(= `role_for` 落在 `WRITE_MEMBER_ROLES`,即 `owner`/`editor`)
- **返回**:`True`/`False`
- **副作用**:无
- **不变量**:**所有写接口都必须经过它**;`viewer` 对共享日历一律 403;
  别人的私人日历 `can_write` 也是 `False`(归属人以外谁都写不了,管理员也不例外),
  但**不可见的日历在路由上通常先撞到可见性判定而返回 404**(不泄露存在性),
  因此同一件事在 HTTP 上可能是 404 而不是 403:详情取决于路由先做哪一步判定
- **测试**:`tests/test_family_accounts.py::test_member_cannot_see_owner_personal_calendar`、
  `::test_admin_cannot_see_member_personal_calendar`、`::test_editor_role_can_write_shared_calendar`、
  `tests/test_family_web.py::test_member_cannot_write_to_shared_calendar`(可见但只读 → 403)、
  `::test_admin_cannot_move_event_into_member_private_calendar`(写进别人私人日历 → 404)

### `CalendarAccess.role_for(calendar_id) -> str | None`

- **位置**:`app/accounts.py:574`
- **用途**:取该日历上的角色
- **返回**:`owner`/`editor`/`viewer`;私人日历的归属人是 `owner`;
  不可见或 `None` 返回 `None`
- **副作用**:无
- **测试**:`tests/test_family_accounts.py::test_editor_role_can_write_shared_calendar`

### `CalendarAccess.viewable_ids() -> list[str]`

- **位置**:`app/accounts.py:591`
- **用途**:本次可见的全部日历 id,**读接口的过滤集合**
- **返回**:按 `calendars` 顺序的 id 列表(私人日历在前,共享日历按排序)
- **副作用**:无
- **不变量**:空列表表示「一条都看不见」,查询层必须**返回空结果而不是不过滤**
  (见 `app/events.py::query_events` 对空列表的处理);
  列表内容只取决于数据库状态,保证导出确定性
- **测试**:`tests/test_family_accounts.py::test_member_cannot_see_owner_personal_calendar`、
  `::test_admin_cannot_see_member_personal_calendar`、
  `::test_events_are_isolated_per_calendar`

### `CalendarAccess.writable_ids() -> list[str]`

- **位置**:`app/accounts.py:594`
- **用途**:本次可写的全部日历 id
- **返回**:id 列表(个人日历 + 被授予 `owner`/`editor` 的共享日历)
- **副作用**:无
- **不变量**:必须是 `viewable_ids()` 的子集
- **测试**:`tests/test_family_accounts.py::test_default_write_calendar_prefers_personal`

### `CalendarAccess.default_write_calendar_id() -> str | None`

- **位置**:`app/accounts.py:597`
- **用途**:新建事件时的默认落库日历
- **返回**:**优先自己的私人日历**,没有可写私人日历时取第一个可写共享日历;
  一个都没有则 `None`
- **副作用**:无
- **不变量**:即使被提升为共享日历的 `editor`,默认仍是自己的私人日历
  (不打扰家庭日历);`None` 时调用方必须拒绝创建而不是随便挑一条日历
- **测试**:`tests/test_family_accounts.py::test_default_write_calendar_prefers_personal`、
  `tests/test_family_web.py::test_member_can_create_in_own_personal_calendar`、
  `::test_calendars_endpoint_lists_visible_calendars_with_roles`(`default_calendar_id`)

### `CalendarAccess.describe(calendar_id) -> str`

- **位置**:`app/accounts.py:605`
- **用途**:给日历 id 一个人可读的名称,用于提示语
- **返回**:日历名;**未知日历返回 `"未知日历"`**(不抛错)
- **副作用**:无
- **测试**:暂无直接测试

### `calendar_access(session, user) -> CalendarAccess`

- **位置**:`app/accounts.py:610`
- **用途**:算出该账号可见的日历与各自角色——一切权限判定的入口
- **返回**:`CalendarAccess`;个人日历只有归属人能进,共享日历按 `calendar_members` 过滤,
  没有成员关系的共享日历**不出现**
- **副作用**:两次查询(私人日历 + 该账号的成员关系),不写库
- **不变量**:私人日历的可见性**只看归属**,与角色、与「是不是管理员」无关;
  这是「管理员也读不到别人私人日历」的实现处,不得放宽
- **测试**:`tests/test_family_accounts.py::test_member_cannot_see_owner_personal_calendar`、
  `::test_admin_cannot_see_member_personal_calendar`、
  `::test_editor_role_can_write_shared_calendar`、`::test_default_write_calendar_prefers_personal`、
  `tests/test_family_web.py::test_calendars_endpoint_lists_visible_calendars_with_roles`

### `visible_calendar_ids(session, user) -> list[str]`

- **位置**:`app/accounts.py:645`
- **用途**:`calendar_access(...).viewable_ids()` 的便捷封装
- **返回**:可见日历 id 列表
- **副作用**:无
- **不变量**:同 `viewable_ids()`
- **测试**:`tests/test_family_accounts.py::test_family_scope_contains_only_shared_calendars`、
  `::test_events_are_isolated_per_calendar`

### `family_calendar_ids(session) -> list[str]`

- **位置**:`app/accounts.py:649`
- **用途**:**家庭共享范围 = 所有共享日历**,外部收件人与家庭订阅地址都用它
- **返回**:共享日历 id 列表(**不含任何私人日历**)
- **副作用**:无
- **不变量**:绝不能把私人日历混进来;它与 `visible_calendar_ids` 是不同概念,
  两者相等只可能是「该账号没有任何共享日历」的巧合
- **测试**:`tests/test_family_accounts.py::test_family_scope_contains_only_shared_calendars`、
  `::test_legacy_events_are_moved_to_owner_personal_calendar`、
  `tests/test_family_web.py::test_family_feed_excludes_personal_calendars`

### `target_calendar_ids(session, *, user_id) -> list[str]`

- **位置**:`app/accounts.py:654`
- **用途**:导出/邮件目标的日历集合:`user_id` 非空 → 该账号的可见范围;
  为空 → 家庭共享范围
- **返回**:id 列表;`user_id` 有值但账号不存在时返回**空列表**(而不是全库)
- **副作用**:无
- **不变量**:账号不存在必须返回空,绝不能退化成「不过滤」;结果只取决于数据库状态
- **测试**:经 `tests/test_family_mail.py::test_resolve_targets_covers_each_enabled_user_and_family`
  间接覆盖(导出侧 `resolve_targets` 用它构造每个目标)

### `target_name(session, *, user_id, base_name) -> str`

- **位置**:`app/accounts.py:667`
- **用途**:给订阅目标生成显示名(设计上用于 `X-WR-CALNAME`):
  无 `user_id` → `"{base} · 家庭共享"`;有 → `"{base} · {user.label}"`
- **返回**:名称字符串;账号不存在时返回 `base_name`
- **副作用**:无
- **不变量**:只取决于数据库状态(不得引入挂钟时间),否则会破坏导出确定性
- **测试**:暂无直接测试(当前导出侧由 `app/exporter.py::FeedTarget.name` 提供名称)

### `sync_user_recipient(session, user) -> SyncRecipient | None`

- **位置**:`app/accounts.py:680`
- **用途**:把账号邮箱同步成一条邮件收件人记录
- **规则**:
  - 先按 `user_id == user.id` 取出**该账号名下所有行**(`:693`–`:697`),**不再用
    `scalar_one_or_none()`**——旧写法在「同一账号名下多行」时抛 `MultipleResultsFound`,
    真机升级时就是它让容器进入重启循环;
  - 恰好一行 → 就用它;**多行** → 只记一条 `warning`(`:698`–`:703`),然后
    **按邮箱匹配**挑选该账号的账号收件人:取 `row.email == user.email` 的那行;
    **认不出就一行都不删**(`:704`–`:709`),绝不误删历史邮箱;
  - **账号没有邮箱,或邮箱还没验证**(`not user.email or not is_verified(user)`,
    `:711`–`:715`)→ 删除该账号的收件人记录,返回 `None`。
    即两个前置条件缺一不可:**有邮箱 _且_ 已验证**;
    清空邮箱是「停止收信」,未验证是「先别发」——验证通过时验证路由会再同步一次;
  - 邮箱已被一条**外部**收件人占用 → **认领**那条记录(改指向该账号,
    `user_id` 与 `scope_user_id` 都设成该账号,保留历史发送时间与未填的显示名),
    不新建第二条;
  - 否则新建或更新:新建时 `user_id` 与 `scope_user_id` 都指向该账号,
    更新时刷新 `scope_user_id` 与邮箱(`:731`–`:748`)。
- **返回**:该账号的 `SyncRecipient`,或 `None`(无邮箱**或**邮箱未验证)
- **副作用**:写/删 `sync_recipients` 并 `flush`(事务由调用方提交);
  账号名下多行时产生 `warning` 日志
- **不变量**:`email` 全表唯一 → 一个邮箱最多一条收件人记录;
  **一个账号最多一行账号收件人**(`user_id` 非空)是这段逻辑的存在理由;
  取行必须容忍重复,认不出时**只告警、不动行**,不得为了让 `scalar_one_or_none()` 成立而删除历史收件人;
  **未验证的账号绝不能成为收件人**——否则日程会发到一个还没确认归属的地址
  (`AGENTS.md` 不变量 22);此函数幂等
- **测试**:`tests/test_family_accounts.py::test_profile_update_syncs_mail_recipient`、
  `::test_account_recipient_claims_matching_external_row`、
  `tests/test_family_mail.py::test_account_and_external_recipients_coexist_and_are_idempotent`、
  `::test_clearing_email_stops_delivery`、
  `tests/test_email_verify.py::test_unverified_account_never_becomes_a_mail_recipient`
  (未验证时一行都不建;验证通过后同一账号立刻能建)、
  `tests/test_family_pages.py::test_recipient_partial_shows_scope`、
  `tests/test_family_upgrade_recipients.py::test_two_legacy_recipients_survive_repeated_bootstrap`、
  `::test_bootstrap_recovers_from_duplicate_account_recipients`(被旧引导写坏的库反复启动不崩)

### `recipient_scope_user_id(recipient) -> str | None`

- **位置**:`app/accounts.py:752`
- **用途**:收件人的附件范围:按**谁的可见范围**取附件
- **返回**:`recipient.scope_user_id or recipient.user_id or None`
  ——账号收件人 → 该账号本人;历史外部邮箱 → 绑定时指定的账号(保持原有收信范围);
  其余外部邮箱 → `None`(只有家庭共享日历)
- **副作用**:无
- **不变量**:范围判定**只看这两个字段的先后顺序**(范围优先于身份),
  不要再额外引入标记列;`user_id` 现在只是身份标记,单独读它当范围是错的
  (见 `AGENTS.md` 不变量 16)
- **测试**:`tests/test_family_accounts.py::test_legacy_recipients_keep_owner_scope_on_first_upgrade`
  (断言 `recipient_scope_user_id(recipient) == owner.id`)、
  `tests/test_family_upgrade_recipients.py::test_two_legacy_recipients_survive_repeated_bootstrap`
  (用它把范围喂给 `target_calendar_ids` 比对首位管理员的可见日历集合)

### `user_timezone(user, settings) -> str`

- **位置**:`app/accounts.py:762`
- **用途**:该账号展示时间用的 IANA 时区
- **参数**:`user` 可为 `None`(未登录页面)
- **返回**:账号自己的时区;**未设置或非法时回退 `settings.default_timezone`**(不抛错)
- **副作用**:无
- **不变量**:库内时间一律 naive UTC,只有展示层做换算;
  非法时区不得让页面 500
- **测试**:`tests/test_family_accounts.py::test_user_timezone_falls_back_to_default`、
  `tests/test_family_pages.py::test_profile_rejects_invalid_timezone`

## 变更

本模块是 2026-10 家庭版改造中**新增**的文件(没有旧版函数需要迁移),但它的引入改变了既有模块的契约:

- `app/deps.py`:当前用户依赖与管理员依赖改为基于 `users` 表,登录会话里带账号 id;
- `app/routers/api_events.py` 与 `app/routers/api_agent.py`:读写事件前一律先取
  `calendar_access(session, user)` 做可见/可写判定(Agent 令牌的归属账号与默认日历见
  `docs/static/app-token-store.md`);
- `app/routers/pages.py`:页面时区改用 `user_timezone(current.user, settings)`,
  成员与日历清单改用 `list_users` / `calendar_access`;
- `app/exporter.py::resolve_targets`:导出目标改为「每个启用账号一个 + 家庭一个」,
  日历集合来自 `target_calendar_ids` / `family_calendar_ids`;
- `app/bootstrap.py::startup_checks`:启动时先跑 `ensure_family_setup`
  (见 `docs/static/app-bootstrap.md`)。
- 数据模型:`users`/`calendars`/`calendar_members` 由迁移 0005 建出
  (见 `docs/static/migrations-0005.md`、`docs/static/app-models.md`);
  `users` 的邮箱验证 5 列由迁移 0007 建出(见 `docs/static/migrations-0007.md`)。
- `app/email_verify.py`:本模块只借它的 `is_verified` 做收件人前置条件
  (「有邮箱且已验证」);验证码的生成/校验与闸门判定都在那边。
- `app/feed_tokens.py`:`create_user(settings=...)` 调 `ensure_account_feed_token`
  登记账号的固定订阅地址;固定地址的派生与解析都在那边。

## 变更记录

- 2026-10-08:新增本文档。归档家庭版账号模块的全部对外契约:账号 CRUD/认证/资料/口令/删除、
  私人日历与共享日历、成员角色、`CalendarAccess` 的可见可写判定、
  `visible_calendar_ids`/`family_calendar_ids`/`target_calendar_ids`、`sync_user_recipient`
  与 `user_timezone`,并写明「管理员也读不到别人私人日历」的隐私不变量。
- 2026-10-11:收件人身份与范围拆分。`recipient_scope_user_id` 改为
  `scope_user_id or user_id or None`;`sync_user_recipient` 改为「按邮箱认领 + 认不出就不动」
  的行选择(不再 `scalar_one_or_none`,多行时只告警并选邮箱匹配的那行,绝不误删);
  `delete_user` 新增把 `scope_user_id == 该账号且 user_id IS NULL` 的行退回家庭共享范围;
  并订正这几个条目的源码行号(`delete_user` / `sync_user_recipient` /
  `recipient_scope_user_id` / `target_calendar_ids` / `target_name` / `user_timezone`;
  本文件其余旧条目的行号在本轮之前就已过期,未一并核对)。
- 2026-10-12:`create_user` 新增可选参数 `settings`(`Settings | None = None`),
  在建号后幂等登记该账号的**固定订阅地址**(`ensure_account_feed_token`,只写 sha256),
  并补写副作用与不变量;`sync_user_recipient` 的前置条件由「有邮箱」收紧为
  「**有邮箱且已验证**」(未验证即删除/不建该行),`None` 返回值的两种原因一并写明;
  `owner_user` 删掉「旧订阅地址 `/calendar` 绑首位管理员」的过期说法,
  测试名更正为 `tests/test_family_web.py::test_owner_fixed_address_serves_owner_scope_not_union`;
  本轮把**全文所有条目的源码行号**按当前文件重校(旧行号整体偏移 1–28 行不等)。