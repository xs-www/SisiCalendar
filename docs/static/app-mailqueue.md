# `app/mailqueue.py`

持久化发信队列。家庭版(2026-10)的变更集中在**按收件人目标取附件**与**正文范围提示**;
队列语义(幂等、退避、重试)未变。**2026-10-12 第二轮**追加「发起人」:正文与任务记录里
标明**这次操作是谁发起的**。

## 函数

### `attachment_source(export, settings, *, target_key) -> tuple[Path, str]`

- **位置**:`app/mailqueue.py:84`
- **用途**:决定某个导出版本对某个**订阅目标**的发信任务要附哪个文件、以及它的 SHA-256
- **参数**:`target_key` 由 `app/exporter.py::feed_target_key(user_id=recipient_scope_user_id(recipient))`
  产生——账号收件人(以及 `scope_user_id` 指向某账号的历史外部邮箱)是 `user-<id>`,
  只有家庭共享范围的外部邮箱是 `family`(见 `app/accounts.py::recipient_scope_user_id`)
- **回退顺序**(都不存在时才继续往下):
  1. `mail_snapshot_path_for(settings, export.version, target_key=target_key)`
     ——**该目标**「仅尚未结束的日程」的邮件快照;
  2. `target_snapshot_path_for(settings, export.version, target_key)`——该目标的完整快照;
  3. `export.snapshot_path` + `export.content_hash`——并集版本快照(升级前的旧版本兜底)。
  这样升级前创建的旧版本、被清理过快照的版本都仍然发得出去,宁可多发历史日程,
  也不能让任务因为找不到文件而失败
- **不变量**:返回的哈希**永远等于实际被附上的那个文件**的哈希,发送前
  `_load_attachment` 会再校验一次;附件必须是**该任务对应目标**的不可变快照,
  重试不得改用更新版本或别的目标的文件
- **副作用**:读文件(不改动)
- **测试**:`tests/test_mail.py::test_attachment_hash_matches_task_snapshot`、
  `::test_attachment_falls_back_to_version_snapshot`、
  `tests/test_family_mail.py::test_each_recipient_attachment_comes_from_its_own_snapshot`、
  `::test_legacy_external_recipient_keeps_owner_scope_attachment`(范围由 `scope_user_id`
  决定的历史外部邮箱拿到首位管理员的可见范围快照)

### `_scope_calendar_ids(session, recipient, cache) -> set[str]`

- **位置**:`app/mailqueue.py:111`(2026-10-12 第四轮新增)
- **用途**:这位收件人**能看见的日历集合**,用于判断「这次改动要不要通知他」
- **规则**:`recipient_scope_user_id(recipient)` 非空 → `target_calendar_ids(user_id=…)`
  (该账号可见范围);为空 → `family_calendar_ids(session)`(家庭共享日历)。
  与附件目标用的是**同一套范围判定**,不会出现「按 A 判断要不要发、按 B 取附件」
- **参数**:`cache` 是 `scope_user_id → 日历集合` 的缓存,同一批收件人只查一次库
- **副作用**:读库(不改动)
- **不变量**:范围判定只能来自 `app/accounts.py` 的那两个函数,**不得**在这里另写一套

### `ensure_delivery_tasks(session, *, export_version, ...) -> int`

- **位置**:`app/mailqueue.py:130`
- **本次变更**:任务的 `snapshot_path` / `attachment_hash` 不再直接取 `CalendarExport`
  的两个字段,而是**按收件人范围**逐目标决定
  - 目标键:`feed_target_key(user_id=recipient_scope_user_id(recipient))`
    (`app/mailqueue.py:151`)——范围由 `scope_user_id or user_id` 决定,账号收件人拿到
    「他可见日历里尚未结束的日程」,`scope_user_id` 指向某账号的历史外部邮箱
    拿到「该账号可见范围里尚未结束的日程」(收信范围与升级前一致),其余外部邮箱拿到
    「家庭共享日历里尚未结束的日程」
  - 2026-10-11 起**不再**直接读 `recipient.user_id`:多行历史绑定被降级后 `user_id` 为空
    但 `scope_user_id` 仍指向首位管理员,只看 `user_id` 会把它们的范围错判成家庭共享
  - 同一目标只算一次:循环内用 `sources: dict[key, (path, hash)]` 缓存,
    多个收件人共用同一个目标时不会重复读文件
- **按可见范围筛选收件人(2026-10-12 第四轮新增)**:新增关键字参数
  `changed_calendar_ids`
  - 传了非空集合 → 只有 `_scope_calendar_ids(...)` 与它**有交集**的收件人才建任务;
    看不见这次改动的收件人直接跳过(否则他会收到一封附件里根本没有这条日程的邮件),
    跳过数量写进 info 日志;
  - 不传 / 传空 → **不筛选**(手动群发、CLI 导出、后台修复拿不到具体日历);
  - `app/sync_service.py::publish` 在没显式给集合时会按 `event.calendar_id` 推断,
    所以「具体日程的增删改」天然带范围;事件在日历之间移动时调用方要把新旧两边都传进来
- **建任务前对齐账号收件人(2026-10-12 第四轮新增)**:先调
  `app/accounts.py::sync_account_recipients(session)` 一次。账号邮箱是「范围内用户的
  已认证邮箱」的唯一来源,但历史库里那行可能被人删掉或停用过;这里在**发信前**重新对齐,
  保证「范围内的家人」一定在收件人集合里,而不是只依赖上一次启动是否修复成功
- **发起人(2026-10-12 第二轮新增)**:新增两个关键字参数
  - `actor_user_id`:发起操作的账号 id。发信时用它现查账号,显示名以**那一刻**为准;
  - `actor_label`:发起人显示名的**快照**,账号删除后仍能说明当时是谁;
  - 落库时 `actor_label` 先 `strip()` 再截断到 100 字符(`snapshot_label`),两侧都为空
    则写 `None`。历史任务、后台修复与 CLI 导出传空值,正文显示「未记录」;
  - **默认值都是 `None`**,现有调用方不需要改动即可继续工作
- **不变量**(全部未变):幂等键仍是 `(export_version, recipient_id)`;
  同一版本对同一收件人只建一个任务;SMTP 未配置或没有启用收件人时返回 0
- **调用方**:`app/sync_service.py::publish`(保存链路)、
  `app/mailqueue.py::send_latest_to_enabled`(设置页「向当前人员发送最新版」,不带筛选)
- **测试**:`tests/test_mail.py::test_task_creation_is_idempotent`、
  `::test_attachment_falls_back_to_version_snapshot`、
  `tests/test_family_mail.py::test_account_and_external_recipients_coexist_and_are_idempotent`、
  `::test_legacy_external_recipient_keeps_owner_scope_attachment`(目标键按范围账号选取)、
  `tests/test_mail_actor.py::test_actor_is_frozen_with_the_task_and_survives_account_deletion`、
  `tests/test_account_recipient_sync.py::test_shared_calendar_change_mails_every_in_scope_verified_account`、
  `::test_personal_calendar_change_skips_out_of_scope_recipients`、
  `::test_multiple_changed_calendars_notify_every_affected_scope`、
  `::test_manual_sync_ignores_the_scope_filter`(手动群发不带筛选取全部)、
  `::test_explicit_calendar_filter_still_applies_to_manual_calls`(显式给日历集合时严格筛选)、
  `::test_publish_restores_a_deleted_account_recipient`(发信前对齐账号收件人)

### `_actor_name(session, delivery) -> str | None`

- **位置**:`app/mailqueue.py:202`
- **用途**:邮件正文与队列里「发起人」那一行要用的名字,认不出返回 `None`
- **取值顺序**:
  1. `delivery.actor_user_id` 非空 → `app/accounts.py::get_user` 现查账号,取
     `user.label`(`display_name or username`)——**界面改了显示名,还没发出去的邮件也跟着变**;
  2. 查不到(账号已删除)或 `actor_user_id` 为空 → 退回任务记录里的 `actor_label` 快照;
  3. 两者都为空 → `None`,调用方渲染成「未记录」。
- **为什么要有快照兜底**:`AGENTS.md` 不变量 20 要求删账号时在应用层清空归属,
  `app/accounts.py::delete_user` 会把 `MailDelivery.actor_user_id` 显式置空;
  这时只剩快照能说明「当时是谁操作的」。若两个字段都没有(升级前的历史任务),
  宁可写「未记录」也不编造名字。
- **不变量**:返回的名字**只**来自账号显示名或快照,绝不去猜事件上的
  `created_by_user_id`(那是「谁建的」,不是「谁改的」);返回值不写日志(只进邮件正文)
- **副作用**:读 `users` 表(不改动)
- **测试**:`tests/test_mail_actor.py::test_actor_name_follows_the_current_display_name`、
  `::test_actor_is_frozen_with_the_task_and_survives_account_deletion`、
  `::test_legacy_task_without_actor_says_unrecorded`

### `_subject_for(...) -> str`(主题文案)

- **位置**:`app/mailqueue.py:189`
- **本次未改**:主题仍是「日程已更新:{title}」「日程已移除:{title}」「日历快照:{calendar}」。
  **发起人只出现在正文与队列里,不改进主题**——邮件列表页一屏多封时,主题保持稳定更易扫读
- **不变量**:主题里不得出现收件人地址、订阅地址或别人的名字

### `_scope_hints(recipient, *, settings, session) -> tuple[str, str]`

- **位置**:`app/mailqueue.py:253`
- **用途**:给出邮件正文里的「附件范围标签」与「订阅地址提示」两个字符串
- **规则**(`app/mailqueue.py:268`–`:283`):
  - 先用 `recipient_scope_user_id(recipient)` 得到范围账号(`scope_user_id or user_id`);
  - **范围非空** → 标签分两种说法:
    - 该行是**账号收件人**(`recipient.user_id` 非空)→ `"你可见的日历(个人 + 家庭共享)"`,
      并且**订阅地址提示 = 他自己那条固定地址**
      (`urls.subscription_url(settings, account_feed_token(settings, recipient.user_id))`);
    - 该行 `user_id` 为空(历史外部邮箱,靠 `scope_user_id` 沿用被绑定账号的范围)→
      `f"{scope_user.label 或 '某成员'}可见的日历(个人 + 家庭共享)"`,订阅地址提示 **空串**
      (`scope_user` 是 `app/accounts.py::get_user(session, scope_user_id)`)。
      正文**不能**把别人范围里的日程说成「你可见的日历」;
  - **范围为空**(`recipient is None` 或只收家庭共享)→ 标签 `"家庭共享日历"`,订阅地址提示空串
- **本次变更(2026-10-12)**:订阅地址的判据从「该行本身是首位管理员的账号收件人」
  改为**「只要是账号收件人,就给本人自己那条固定地址」**——固定地址由服务端派生、
  随时可重算,所以正文里能直接写出明文,不再受「只显示一次」的限制;
  **旧 `SUBSCRIPTION_URL` 分支整体删除**。外部邮箱(含 `scope_user_id = 某账号` 的历史邮箱)
  **一条地址都不给**:地址只属于账号本人;标签仍按 `label` 显示,不对历史邮箱说「你可见的日历」
- **返回**:两个字符串都可能为空,调用方据此决定是否渲染订阅地址那一行
- **不变量**:**并集地址与旧 `SUBSCRIPTION_URL` 都已经彻底取消**,任何邮件里都不得出现;
  正文里的地址**只能**是该账号自己那条固定地址(别人的地址、家庭令牌地址都不许出现);
  地址明文只允许出现在**邮件正文**里,不得写日志与审计
  (`AGENTS.md` 不变量 16、18、23)
- **测试**:`tests/test_family_mail.py::test_owner_recipient_gets_his_own_fixed_address`
  (首位管理员的账号收件人拿到**自己**的固定地址;并集式的无令牌地址
  ——正文里「基地址后直接换行」那种——不出现)、
  `::test_member_recipient_gets_only_his_own_address`
  (成员拿到自己的地址,正文里没有首位管理员的地址)、
  `::test_legacy_external_recipient_keeps_owner_scope_attachment`
  (历史外部邮箱:附件仍是首位管理员的可见范围,正文出现「家庭管理员可见的日历」、
  **不出现**「你可见的日历」、**不含**任何 `sisi_feed_` 地址)

### `_body_for(...) -> str`(正文文案)

- **位置**:`app/mailqueue.py:221`
- **本次变更**:参数 `subscription_url` 由两个参数取代——
  `scope_label`(附件范围)与 `subscription_hint`(订阅地址或空串)
  - 附件说明改为「附件 calendar.ics 是**{scope_label}**里尚未结束的日程快照……已经结束的
    日程不在附件里——包括今天早些时候刚结束的。完整历史(含过去事件)始终可以从长期订阅
    地址获得。」
  - `subscription_hint` 非空(只可能是账号收件人)→
    `你的固定订阅地址(长期有效,需要 Tailscale):<地址>`;
    为空 → 「长期订阅地址:每位家庭成员都有自己的固定地址(每人一条、只在本人界面显示),
    主人可以在日历管理界面「我的订阅地址」里查看。」
- **2026-10-12 第二轮变更(发起人一行)**:新增可选参数 `actor_label`
  - 只在 `delivery.change_type in (CHANGE_CREATED, CHANGE_UPDATED, CHANGE_DELETED)` 时
    追加一行 `发起人:{actor_label or '未记录'}`,位置紧跟在 `本次操作:{action}` 之后
    (`app/mailqueue.py:247`);
  - **手动同步(`CHANGE_MANUAL`)不写这一行**:「向当前人员发送最新版」「重新生成 ICS」
    是管理员主动群发,收件人关心的是日历本身,说成「某某操作的」没有意义;
    取不到发起人时也不会写「未记录」——不存在的操作人无从谈起
  - 名字由 `attempt_delivery` 用 `_actor_name(session, delivery)` 算好后传入,
    **正文里的名字是发信那一刻的账号显示名**(与队列里的快照可能不同,这是有意的)
- **不变量**:正文仍包含本次操作类型、事件标题、事件时间、日历生成时间(UTC)、订阅地址段;
  **不再出现并集订阅地址,也不出现旧 `SUBSCRIPTION_URL`**;发起人一行**不得**成为
  「谁建的日程」的同义词(见 `_actor_name`);文案由
  `tests/test_mail.py::test_each_recipient_gets_separate_message_with_future_attachment`
  (`长期订阅地址`、`尚未结束的日程`、`完整历史(含过去事件)`、正文里没有 `sisi_feed_`)与
  `tests/test_mail_smtp.py::test_real_smtp_conversation` 断言关键短语;
  发起人相关的四条断言见 `tests/test_mail_actor.py`
  (`发起人:家庭管理员`、`发起人:思思`、`发起人:未记录`、手动同步正文里 `发起人` 不出现)
- **2026-10-12 订正**:「已结束的日程不在附件里」这句旧措辞已经不存在了 —— 现在的说法是
  「附件是**<范围>**里尚未结束的日程快照。**完整历史(含过去事件)始终可以从长期订阅地址
  获得**。」测试里对应的断言也从 `已经结束的日程不在附件里` 改为
  `完整历史(含过去事件)`(旧断言在 2026-10-12 的真机回归里暴露为失败,见下文变更记录)
- **调用方**:`attempt_delivery`(`app/mailqueue.py:297`)——发信前用
  `session.get(SyncRecipient, delivery.recipient_id)` 取回收件人再算 `_scope_hints`

## 影响面

- 附件不再是发布文件的副本,因此 `MailDelivery.attachment_hash` 通常**不等于**
  `CalendarExport.content_hash`。开发文档验收 #10 的措辞已同步改为「附件取自该任务的
  不可变快照,哈希与任务记录一致」。
- 每个版本的快照是一整套:`calendar-v{N}.ics`(并集,内部)、
  `calendar-v{N}-<目标键>.ics` 与 `calendar-v{N}-<目标键>-upcoming.ics`。
  `cleanup_snapshots` 按版本前缀一次清干净,见 `docs/static/app-exporter.md`。
- `templates/partials/sync_status.html` 的二次确认文案同步改为「尚未结束的日程快照」;
  表格在 2026-10-12 第二轮增列「发起人」(`delivery.actor_label`,历史任务显示 `—`),
  见 `docs/static/templates.md`。
- 「发起人」把「发信时的名字」与「任务里的快照」分开了:邮件正文用 `actor_user_id`
  现查账号(改名立刻生效),队列用 `actor_label` 快照(还原当时是谁)。两个口径都只
  出现在给收件人与管理员的产物里,不进日志。
- **收件人集合本身也是派生的**(2026-10-12 第四轮):账号邮箱来自 `users`,
  外部邮箱来自 `sync_recipients` 里 `user_id IS NULL` 的行。前者的增删改都由
  `sync_account_recipients` 自动维护,设置页只碰后者。

## 变更记录

- 2026-10-06:附件改为取自「仅尚未结束的日程」的邮件快照,缺失时回退版本快照;正文提示语同步更新。
- 2026-10-06(同日):过滤分界由「今天 00:00」收紧为「此刻」,当天已结束的日程也不再进附件;
  正文改为「尚未结束的日程……已经结束的日程不在附件里」。
- 2026-10-08:家庭版按收件人目标取附件 —— `attachment_source` 新增 `target_key`,
  回退链变为「目标邮件快照 → 目标完整快照 → 并集版本快照」;
  `ensure_delivery_tasks` 按收件人范围逐目标建任务(同目标缓存一次);
  新增 `_scope_hints`,`_body_for` 改收 `scope_label` / `subscription_hint`,
  正文只有首位管理员能看到旧并集订阅地址。
- 2026-10-11:范围改由 `recipient_scope_user_id(recipient)`(`scope_user_id or user_id`)决定 ——
  `ensure_delivery_tasks` 的目标键与 `_scope_hints` 都改成用它;
  `_scope_hints` 的订阅地址判据收紧为「该行本身是首位管理员的账号收件人」,
  `scope_user_id = 首位管理员` 的历史外部邮箱不再拿到并集地址。
  (**已被 2026-10-12 的固定地址模型取代**:见下一条。)
- 2026-10-12:正文订阅地址改为「**账号收件人拿到自己那条固定地址**」——
  `_scope_hints` 用 `account_feed_token(settings, recipient.user_id)` 现算地址
  (固定地址由服务端密钥派生,随时可重算),旧 `SUBSCRIPTION_URL` 分支整体删除;
  **外部邮箱(含 `scope_user_id` 指向某账号的历史邮箱)一条地址都不给**。
  `_body_for` 的提示语同步改为「你的固定订阅地址(长期有效,需要 Tailscale)」与
  「每位家庭成员都有自己的固定地址(每人一条、只在本人界面显示)」;
  测试名随之改为 `tests/test_family_mail.py::test_owner_recipient_gets_his_own_fixed_address`
  与 `::test_member_recipient_gets_only_his_own_address`(旧名
  `test_owner_recipient_may_reuse_legacy_subscription_url` /
  `test_member_recipient_never_sees_legacy_url` 已不再存在)。
- 2026-10-12(第二轮,用户反馈「同步邮件看不出是谁改的」):新增**发起人**。
  `ensure_delivery_tasks` 收 `actor_user_id` / `actor_label` 并落库(迁移 `0008`);
  新增 `_actor_name`(现查账号 → 快照兜底 → `None`);
  `_body_for` 新增 `actor_label` 参数,**只在新增/修改/删除日程**时写一行
  `发起人:…`,手动同步不写、也从不写「未记录」;
  `send_latest_to_enabled` 透传两个参数(手动同步也记录操作者,供队列显示);
  `app/accounts.py::delete_user` 显式把 `MailDelivery.actor_user_id` 置空(不变量 20 的同一理由)。
  测试新增 `tests/test_mail_actor.py`(10 项)。
- 2026-10-12(第四轮,用户反馈「共享日历变动没有同步到范围内用户的已认证邮箱」):
  `ensure_delivery_tasks` 新增 `changed_calendar_ids` 与 `_scope_calendar_ids()` ——
  只给「可见范围包含这次改动的日历」的收件人建任务;不传即不筛选(手动群发/CLI/后台修复)。
  建任务之前先调 `app/accounts.py::sync_account_recipients()` 对齐账号收件人,
  让「范围内的家人」不再依赖上一次启动是否修复成功。
  测试新增 `tests/test_account_recipient_sync.py`(14 项)。