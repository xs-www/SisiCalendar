# `app/models.py`(家庭版新增与变更)

本模块定义全部数据表、角色/日历类型常量与账号名规范化:账号(`users`)、日历(`calendars`)、
共享日历成员(`calendar_members`)、邀请码(`invite_codes`)、只读订阅令牌(`feed_tokens`),
以及给既有表补上的归属列(事件的 `calendar_id`,收件人的 `user_id` 身份与 `scope_user_id`
范围,Agent 令牌的 `user_id`)。
本轮(2026-10-12)给 `users` 补上**邮箱验证**的 5 列。
私有/共享的可见性判定见 `docs/static/app-accounts.md`,历史数据回填见 `docs/static/app-bootstrap.md`,
结构变更见 `docs/static/migrations-0005.md`(家庭版建表)、`docs/static/migrations-0006.md`
(收件人身份与范围拆分)与 `docs/static/migrations-0007.md`(邮箱验证列)。

时间基调不变:**所有 `created_at` / `updated_at` / `last_login_at` / `last_used_at` / `expires_at`
都是 naive UTC**(`app/timeutil.py::utcnow`),展示与 ICS 生成时再按账号或应用时区还原。

## 新增

### 常量:角色、日历类型与成员权限

- **位置**:`app/models.py:41`–`app/models.py:69`
- **取值**:
  - `ROLE_ADMIN = "admin"` / `ROLE_MEMBER = "member"`;`USER_ROLES` 是二者的元组,
    也是数据库 `CheckConstraint("role IN ('admin','member')")` 的取值集合。
    `admin` 表示「能管账号、邀请码、共享日历与全局设置」,**不表示能读别人的私人日历**。
  - `CALENDAR_KIND_PERSONAL = "personal"` / `CALENDAR_KIND_SHARED = "shared"`;
    `CALENDAR_KINDS` 是二者元组,对应 `calendars.kind` 的检查约束。
  - `MEMBER_ROLE_OWNER = "owner"` / `MEMBER_ROLE_EDITOR = "editor"` / `MEMBER_ROLE_VIEWER = "viewer"`;
    `CALENDAR_MEMBER_ROLES` 是三者元组,对应 `calendar_members.role` 的检查约束。
  - `WRITE_MEMBER_ROLES = (owner, editor)`:**唯一可写判定集合**,`CalendarAccess.can_write`
    只认它;`viewer` 看得到、改不了(新成员默认值)。
  - `FAMILY_FEED_LABEL = "家庭共享日历"`:订阅令牌 `user_id` 为 `NULL` 时的范围名称。
  - `DEFAULT_USER_COLORS`:`#2563eb`/`#db2777`/`#059669`/`#f59e0b`/`#7c3aed`/`#0891b2` 六色循环,
    新建账号与新建日历按顺序取,保证同一家庭的日历在界面上可区分。
- **不变量**:这些字符串同时是数据库检查约束与业务判定的取值来源,**不要在别处硬编码字面量**;
  新增角色/类型必须同时改常量与迁移里的 `CheckConstraint`。
- **测试**:约束取值由 `tests/test_family_accounts.py::test_membership_validation`(非法角色被拒)
  与 `::test_bootstrap_creates_first_admin_and_two_calendars`(`kind='personal'`/`'shared'`)间接覆盖

### `normalize_username(value) -> str`

- **位置**:`app/models.py:110`
- **用途**:账号名统一小写并去首尾空白
- **参数**:任意字符串(空串返回空串)
- **返回**:规范化后的账号名
- **副作用**:无
- **不变量**:账号名比较与唯一性判定一律先过这个函数,避免 `Mama` 与 `mama` 变成两个账号;
  它**不做**格式校验(格式见 `app/accounts.py::validate_username`)
- **测试**:`tests/test_family_accounts.py::test_duplicate_username_is_rejected`

### `User`(表 `users`)

- **位置**:`app/models.py:115`;迁移 `migrations/versions/0005_family_accounts.py`
- **用途**:家庭账号,私人日历的归属主体
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `username`:≤64 字符,**唯一索引**(`ix_users_username`);库内统一小写
  - `display_name`:≤100 字符,可空
  - `email`:≤320 字符,可空;**只有普通索引,唯一性是应用层约束**
    (`app/accounts.py::email_taken` 在写之前查重)
  - `password_hash`:Argon2id 摘要,≤255 字符
  - `role`:`admin` / `member`,默认 `member`,受 `ck_users_role` 约束
  - `color`:界面与 ICS 用的颜色,默认取 `DEFAULT_USER_COLORS[0]`;个人日历创建时跟随该值
  - `timezone`:账号自己的 IANA 时区;**`NULL` 表示跟随应用默认时区**
  - `enabled`:停用后不能登录,也会从导出/邮件目标里消失
  - `must_change_password`:管理员创建或重置口令后置位,首次登录强制改密
  - `created_at` / `updated_at` / `last_login_at`:naive UTC
  - 邮箱验证 5 列(2026-10-12 新增,见下方单独条目):`email_verified_at`、
    `email_verify_code_hash`、`email_verify_expires_at`、`email_verify_attempts`、
    `email_verify_sent_at`
- **约束**:`CheckConstraint("role IN ('admin','member')", name="ck_users_role")`、
  `Index("ix_users_enabled", "enabled")`
- **属性**:`is_admin`(`role == ROLE_ADMIN`)、`label`(`display_name or username`,
  页面与日历名用它,因此**改显示名会改变展示,不应作为主键或匹配依据**)
- **不变量**:`username` 不得重复;`password_hash` 是唯一的口令凭证,明文永不入库
- **测试**:`tests/test_family_accounts.py::test_bootstrap_creates_first_admin_and_two_calendars`、
  `::test_duplicate_username_is_rejected`、`::test_email_must_be_unique`、
  `::test_disabled_account_cannot_authenticate`

### `User` 的邮箱验证列(2026-10-12 新增)

- **位置**:`app/models.py:140`–`app/models.py:146`;迁移
  `migrations/versions/0007_email_verification.py`(见 `docs/static/migrations-0007.md`)
  - `email_verified_at`(`app/models.py:141`):验证通过**那一刻**的 naive UTC;
    **为空且有邮箱 = 待验证**
  - `email_verify_code_hash`(`:143`):6 位验证码的 **HMAC**(键是服务端密钥,
    `app/email_verify.py::code_hash`),**绝不存明文**;≤128 字符
  - `email_verify_expires_at`(`:144`):验证码失效时刻的 naive UTC
    (默认 `now + email_code_ttl_seconds`,30 分钟)
  - `email_verify_attempts`(`:145`):已失败次数,`not null`、默认 `0`;
    第 `email_code_max_attempts`(5)次失败时验证码**直接作废**(置空哈希与有效期)
  - `email_verify_sent_at`(`:146`):上次发送验证码的时刻,用于 60 秒重发冷却
    (`app/email_verify.py::resend_wait_seconds`)
- **用途**:让「你这个邮箱到底归不归你」成为数据库里可判定的状态,而不是只靠一次 HTTP 请求;
  它决定成员能不能使用系统(`app/deps.py::_gate_verification`)与能不能成为邮件收件人
  (`app/accounts.py::sync_user_recipient`)
- **不变量**:
  - `email_verify_code_hash` 里**永远只有 HMAC**,明文只允许出现在验证码邮件正文里,
    不进日志、不进审计 detail、不进数据库;
  - **改邮箱必然回到待验证**:`app/email_verify.py::mark_unverified` 一次把
    `email_verified_at` 与四个验证码字段全部清空;
  - 验证码与**账号 id** 绑定(`code_hash(settings, user_id=..., code=...)`),
    换账号或轮换 `data/session_secret` 都会让存储的哈希失效;
  - `email_verify_attempts` 只增不减直到验证成功或换码时清零,**不得**用它做限流以外的判断
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`、
  `::test_wrong_code_is_counted_and_then_invalidated`(第 5 次失败后哈希与有效期被置空)、
  `::test_expired_code_is_rejected`、`::test_resend_cooldown_then_new_code`、
  `::test_verified_member_who_changes_email_must_verify_again`、
  `::test_unverified_account_never_becomes_a_mail_recipient`

### `Calendar`(表 `calendars`)

- **位置**:`app/models.py:165`
- **用途**:一条日历;**私人日历每人一条,共享日历全家按角色访问**
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `name`:≤100 字符
  - `kind`:`personal` / `shared`,默认 `personal`,有索引并受 `ck_calendars_kind` 约束
  - `owner_user_id`:私人日历的归属人;**共享日历为 `NULL`**;
    外键 `users.id` `ondelete="CASCADE"`(删账号时私人日历由数据库连带删除)
  - `color`、`sort_order`:界面排序与配色,共享日历按创建顺序编号
  - `created_at` / `updated_at`:naive UTC
- **属性**:`is_personal`(`kind == 'personal'`)
- **不变量**:`kind='personal'` 的行必须且只须有一个 `owner_user_id`;`kind='shared'` 的行不得有归属人;
  「谁是成员」不在这张表里,而在 `calendar_members`
- **测试**:`tests/test_family_accounts.py::test_bootstrap_creates_first_admin_and_two_calendars`、
  `::test_delete_user_removes_personal_calendar_but_keeps_shared_events`

### `CalendarMember`(表 `calendar_members`)

- **位置**:`app/models.py:200`
- **用途**:共享日历的成员与角色映射
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `calendar_id` / `user_id`:外键,均 `ondelete="CASCADE"` 且有独立索引
  - `role`:`owner` / `editor` / `viewer`,默认 `viewer`,受 `ck_calendar_members_role` 约束
  - `created_at` / `updated_at`:naive UTC
- **约束**:`UniqueConstraint("calendar_id", "user_id", name="uq_calendar_members_pair")`——
  同一人同一日历只能有一行角色
- **不变量**:**私人日历不使用这张表**(它的可见性只看 `calendars.owner_user_id`),
  因此本表里的成员关系不会泄漏私人日历;每行只表达「这条共享日历上的角色」
- **测试**:`tests/test_family_accounts.py::test_shared_calendar_creator_becomes_owner_and_members_are_viewers`、
  `::test_editor_role_can_write_shared_calendar`

### `InviteCode`(表 `invite_codes`)

- **位置**:`app/models.py:230`
- **用途**:成员自助注册的一次性/多次邀请码
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `code_hash`:`sha256(明文)` 的 64 位十六进制,**唯一索引**(`ix_invite_codes_code_hash`)
  - `label`:给人看的名称,≤100 字符,必填
  - `max_uses`:允许使用次数,默认 1;`used_count`:已使用次数,默认 0
  - `expires_at`:可空,naive UTC;**`NULL` 表示不过期**
  - `enabled`:停用后不可再注册;用尽次数时由 `consume` 自动置 `False`
  - `created_by_user_id`:创建者账号 id,**只记 id、不建外键**(管理员被删不影响邀请码记录)
  - `created_at` / `updated_at`:naive UTC
- **属性**:`remaining_uses` = `max(0, max_uses - used_count)`
- **不变量**:**表内永不出现邀请码明文**;`code_hash` 唯一;`used_count` 只增不减
- **测试**:`tests/test_family_accounts.py::test_invite_is_stored_hashed_and_looked_up_by_hash`、
  `::test_invite_respects_max_uses`、`::test_invite_registration_creates_member`

### `FeedToken`(表 `feed_tokens`)

- **位置**:`app/models.py:263`
- **用途**:一枚令牌对应一个只读 ICS 订阅地址
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `user_id`:**有值 = 该成员可见范围(私人 + 共享)的合并日历;`NULL` = 家庭共享日历范围**;
    外键 `users.id` `ondelete="CASCADE"`,有索引
  - `label`:≤100 字符;**每个账号那条固定地址的标签恒为
    `app/feed_tokens.py::ACCOUNT_FEED_LABEL = "账号订阅地址(固定)"`**
  - `token_hash`:`sha256(明文)`,**唯一索引**(`ix_feed_tokens_token_hash`)
  - `enabled`:停用/撤销后订阅入口立即 404
  - `created_at` / `updated_at` / `last_used_at`:naive UTC;`last_used_at` 由订阅入口每次拉取时刷新
- **不变量**:
  - 库内只存哈希;`user_id` 的两种取值是唯一的范围表达,**不要为「家庭范围」另加标记字段**;
  - **固定地址是「明文一次性」规则的唯一例外**:它的明文由服务端密钥派生
    (`app/feed_tokens.py::account_feed_token`,账号创建时/引导回填各登记一行),
    所以页面可以重新算出并长期显示,但**库里仍然只有 `token_hash`**,明文不得写进日志或审计;
  - 除固定地址外,其余令牌的明文只在创建成功那一次响应出现
    (见 `AGENTS.md` 不变量 18 与 23)
- **测试**:`tests/test_family_accounts.py::test_feed_token_roundtrip`、
  `::test_family_feed_token_has_no_user`、`::test_disabled_feed_token_cannot_be_resolved`、
  `::test_account_feed_token_is_stable_and_derived`、`::test_feed_tokens_are_listed_per_scope`

## 变更

以下条目里的 `ondelete` 是**模型层声明**:按模型新建的库(`Base.metadata.create_all`,测试库走这条路)
由数据库执行;由迁移 0005 升级上来的既有库里,这些**新增列不携带外键约束**
(实测 `sqlite_master` 里 `events`/`sync_recipients`/`agent_tokens` 都没有 `FOREIGN KEY` 行),
细节见 `docs/static/migrations-0005.md`。

### `Event.calendar_id` / `Event.created_by_user_id`

- **位置**:`app/models.py:310` / `app/models.py:313`(新增列);
  复合索引 `Index("ix_events_calendar_start", "calendar_id", "deleted_at", "start_at_utc")`
  在 `app/models.py:360`
- **用途**:给每个事件记上归属日历与创建人,使「可见/可写」完全由日历推导
- **本次变更**(2026-10):既有的单用户事件原本没有日历归属,现在必须属于一条日历
- **`calendar_id` 为 `NULL` 的语义**:
  - 表示**升级尚未完成的生命周期中间态**(0005 迁移已加列但引导还没跑),不是正常业务状态;
  - 任何按可见范围过滤的查询(`Event.calendar_id.in_(calendar_ids)`)都**匹配不到它**,
    因此它不属于任何人的可见范围,订阅与页面都看不见;
  - 只有**不传 `calendar_ids`** 的运维/一致性路径(全库并集产物 `export/calendar.ics`)会带上它,
    所以 `app/accounts.py::delete_user` 必须把个人日历里的日程一并删掉,避免留下这种孤儿事件;
  - `app/bootstrap.py::ensure_family_setup` 每次启动都会把 `calendar_id IS NULL` 的历史事件
    回填到首位管理员的私人日历,因此长期状态里不应存在 `NULL`。
- **约束**:外键 `calendars.id` `ondelete="SET NULL"`;`created_by_user_id` 外键 `users.id`
  `ondelete="SET NULL"`(删账号后共享日历里的历史事件保留,创建人置空)
- **不变量**:新事件必须带 `calendar_id`;`created_by_user_id` 只做审计与展示,不参与权限判定
- **测试**:`tests/test_family_accounts.py::test_legacy_events_are_moved_to_owner_personal_calendar`、
  `::test_delete_user_removes_personal_calendar_but_keeps_shared_events`、
  `::test_events_are_isolated_per_calendar`、`tests/test_family_web.py::test_admin_can_move_event_between_calendars`

### `SyncRecipient.user_id` / `SyncRecipient.scope_user_id`

- **位置**:`app/models.py:395`(`user_id`)、`app/models.py:399`(`scope_user_id`),
  类文档串在 `app/models.py:371`–`app/models.py:384`
- **用途**:把收件人的**身份**与**收信范围**分成两列:身份决定「这行是不是某个账号的
  账号收件人」,范围决定「附件按谁的可见日历裁剪」
- **语义**:
  - `user_id` = **身份**:**非空 = 账号收件人**(由 `users.email` 派生,每个账号至多一行),
    附件是该账号可见范围内「尚未结束」的日程;**`NULL` = 外部邮箱**(没有登录能力);
  - `scope_user_id` = **范围**:非空 = 沿用该账号的可见范围;`NULL` = 只收家庭共享日历。
    账号收件人的范围就是他自己;单用户版升级上来的历史外部邮箱靠它保持「收到全部日程」;
  - 二者统一由 `app/accounts.py::recipient_scope_user_id` 解释为
    `scope_user_id or user_id or None`——**不要在别处直接拿 `user_id` 当范围**
- **为什么必须分开(2026-10-11 真机 Docker 升级缺陷)**:`0005_family_accounts` 只加了
  `user_id`,它后面的启动引导把**每一个**历史外部收件人都写成 `user_id = 首位管理员`,
  于是同一账号名下出现多行;第二次启动时 `sync_user_recipient` 用
  `scalar_one_or_none()` 取行,抛 `MultipleResultsFound`,应用启动失败、容器重启循环。
  把身份与范围压在**同一列**上,就必然在「一个账号一行账号收件人」与
  「任意多个邮箱沿用该账号范围」之间二选一——两者不可能同时成立
- **约束**:两列在模型层都是外键 `users.id` `ondelete="SET NULL"`(删账号时记录保留、
  退回外部语义);由迁移 0006 升级上来的库里 `scope_user_id` **不携带外键**
  (见 `docs/static/migrations-0005.md` 的「已实测」一节、`docs/static/migrations-0006.md`);
  普通索引 `ix_sync_recipients_user_id`、`ix_sync_recipients_scope_user_id`;
  `email` 仍是全表唯一索引
- **不变量**:
  - `email` 唯一 → 一个邮箱最多一条收件人记录(`app/accounts.py::sync_user_recipient`
    会在邮箱相同时认领既有行而不是新建);
  - **`user_id` 非空的行每个账号至多一条**——这正是 `0005`→`0006` 修复的核心,
    也是 `AGENTS.md` 不变量 21;
  - **`user_id` 非空的行是派生数据**(2026-10-12 第四轮):它由
    `users.email` + `users.enabled` + `email_verified_at` 唯一决定,
    由 `app/accounts.py::sync_account_recipients` 维护;设置页与
    `/api/v1/sync-recipients` 只负责 `user_id IS NULL` 的外部邮箱,
    账号行既不能删也不能停用(两处都 409)。停用账号只会让该行 `enabled=False`,
    重新启用账号时自动恢复
- **测试**:`tests/test_family_accounts.py::test_profile_update_syncs_mail_recipient`、
  `::test_account_recipient_claims_matching_external_row`、
  `::test_legacy_recipients_keep_owner_scope_on_first_upgrade`(断言
  `scope_user_id == owner.id` 且 `user_id is None`)、
  `tests/test_family_upgrade_recipients.py::test_two_legacy_recipients_survive_repeated_bootstrap`、
  `::test_bootstrap_recovers_from_duplicate_account_recipients`、
  `tests/test_family_mail.py::test_external_recipient_only_gets_family_calendar`、
  `::test_account_recipient_gets_own_visible_scope`、`::test_clearing_email_stops_delivery`

### `AgentToken.user_id` / `AgentToken.calendar_id`

- **位置**:`app/models.py:514` / `app/models.py:518`(新增列)
- **用途**:让每枚远程接入令牌绑定一个账号与一条默认落库日历,使 Agent 写入也有权限边界
- **语义**:`user_id` **为空**的历史令牌在引导时补成首位管理员,请求时也按首位管理员兜底解释;
  `calendar_id` **为空**表示「用归属账号的私人日历」,非空时是令牌固定的落库日历
- **约束**:`user_id` 外键 `users.id` `ondelete="SET NULL"`,普通索引 `ix_agent_tokens_user_id`;
  `calendar_id` **只存 id、不建外键**(与邀请码的创建人字段同样处理);
  `token_hash` 的唯一索引与「只存哈希」要求不变(见 `docs/static/app-token-store.md`)
- **不变量**:令牌写入的日历必须落在归属账号可写的日历内,否则请求被拒;
  **不得**因为令牌带 `calendar_id` 就绕过 `CalendarAccess` 判定
- **测试**:`tests/test_family_accounts.py::test_agent_token_carries_scope`、
  `::test_legacy_agent_token_is_bound_to_owner`、
  `tests/test_family_mail.py::test_agent_token_writes_into_its_bound_calendar`、
  `::test_agent_token_cannot_read_another_users_event`、`::test_agent_token_without_write_permission_is_rejected`

## 关键语义速查

| 判定 | 依据 | 结果 |
|---|---|---|
| 某账号能否看见某日历 | 私人 = 归属人本人;共享 = 有 `calendar_members` 行 | 否 → 读 404 |
| 某账号能否写某日历 | 私人 = 归属人本人(角色视为 `owner`);共享 = `owner`/`editor` | 否 → 写 403 |
| 订阅令牌范围 | `feed_tokens.user_id` 有值 = 该账号可见范围;`NULL` = 共享日历 | — |
| 账号的固定订阅地址 | `token_hash == sha256(account_feed_token(settings, user_id))` 且 `user_id` 非空 | 界面不给停用/撤销(409) |
| 账号能不能用系统 | `users.email` 非空**且** `email_verified_at` 为空 | 待验证 → 页面 303 到验证页、`/api` 403 `email_not_verified`;**管理员豁免** |
| 账号能不能当邮件收件人 | 同上(有邮箱 **且** 已验证) | 否则 `sync_user_recipient` 删除/不建该行 |
| 邮件收件范围 | `app/accounts.py::recipient_scope_user_id` = `scope_user_id or user_id`;有值 = 该账号可见范围 | `NULL` = 共享日历 |
| 谁来维护收件人 | `user_id` 非空 = 账号资料的派生结果(`sync_account_recipients` 自动对齐,界面/接口都不可增删启停) | `user_id` 为空 = 设置页维护的外部邮箱 |
| 一次改动发给谁 | `app/mailqueue.py::_scope_calendar_ids(recipient)` 与本次改动的日历集合有交集 | 无交集 → 不建发信任务 |
| 事件无人可见 | `events.calendar_id IS NULL` | 仅升级中间态,引导会回填 |

## 测试

`tests/test_family_accounts.py`(领域层)、`tests/test_family_web.py`(HTTP 与订阅)、
`tests/test_family_mail.py`(邮件范围与 Agent 作用域)、`tests/test_family_pages.py`(页面与管理员操作)。

## 变更记录

- 2026-10-08:新增本文档。归档家庭版新增的 `users`/`calendars`/`calendar_members`/`invite_codes`/
  `feed_tokens` 与角色、日历类型、成员权限常量,以及 `events`、`sync_recipients`、`agent_tokens`
  新增的归属列与 `calendar_id IS NULL` 的中间态语义。
- 2026-10-11:`SyncRecipient` 的身份与范围拆成 `user_id`(身份,每账号一行)与
  `scope_user_id`(范围,可指向任意账号);补写「为什么必须分开」的真机升级缺陷
  (`MultipleResultsFound` 导致容器重启循环)、`recipient_scope_user_id` 的统一解释规则,
  并同步「关键语义速查」的邮件收件范围一行。
- 2026-10-12:`users` 新增邮箱验证 5 列(`email_verified_at`、`email_verify_code_hash`、
  `email_verify_expires_at`、`email_verify_attempts`、`email_verify_sent_at`,迁移 0007),
  写明用途与不变量(HMAC 落库、改邮箱必然回到待验证、验证码与账号 id 绑定、尝试次数语义);
  `FeedToken` 补写固定地址的标签与「明文一次性规则的唯一例外」;
  「关键语义速查」新增固定地址、邮箱验证闸门与账号收件人前置条件三行;
  并按当前源码校正 `Calendar`/`CalendarMember`/`InviteCode`/`FeedToken`/`Event`/
  `SyncRecipient`/`AgentToken` 的行号。
- 2026-10-12(第四轮):`SyncRecipient` 补写「账号收件人是派生数据」这条不变量
  (由 `sync_account_recipients` 维护,设置页/接口只碰外部邮箱;停用账号只停行不删行),
  「关键语义速查」新增「谁来维护收件人」与「一次改动发给谁」两行。