# `migrations/versions/0005_family_accounts.py`

本迁移把单用户库结构升级为家庭版:新建账号、日历、成员、邀请码与订阅令牌五张表,
并给 `events`、`sync_recipients`、`agent_tokens` 补上归属列与索引。
它**只做结构变更,不动任何既有数据**;数据回填由启动引导完成(见 `docs/static/app-bootstrap.md`)。
**下一版迁移是 [`0006_recipient_scope`](migrations-0006.md)**:它给 `sync_recipients` 补上
`scope_user_id` 并把「身份」与「范围」拆开,本迁移新增的 `user_id` 从此**只表示身份**
——不要再用它当收信范围。

## 元信息

- **Revision ID**:`0005_family_accounts`(`migrations/versions/0005_family_accounts.py:29`)
- **Revises**:`0004_event_reminders`(`migrations/versions/0005_family_accounts.py:30`)
- **Create Date**:2026-10-07
- **入口**:`upgrade()` 在 `migrations/versions/0005_family_accounts.py:35`,
  `downgrade()` 在 `migrations/versions/0005_family_accounts.py:156`
- **测试**:暂无直接测试(迁移由 `app/bootstrap.py::run_migrations` 在启动时执行;
  `tests/` 里的家庭版用例直接按当前模型建库,不逐个校验迁移脚本)

## 新增

### 表 `users`

- **位置**:`migrations/versions/0005_family_accounts.py:37`
- **列**:`id`(PK,`String(36)`)、`username`、`display_name`、`email`、`password_hash`、
  `role`、`color`、`timezone`、`enabled`、`must_change_password`、`created_at`、`updated_at`、
  `last_login_at`
- **约束**:`CheckConstraint("role IN ('admin','member')", name="ck_users_role")`
- **索引**:`ix_users_username`(唯一)、`ix_users_email`(非唯一,唯一性由应用层保证)、
  `ix_users_enabled`

### 表 `calendars`

- **位置**:`migrations/versions/0005_family_accounts.py:60`
- **列**:`id`(PK)、`name`、`kind`、`owner_user_id`、`color`、`sort_order`、`created_at`、`updated_at`
- **约束**:`CheckConstraint("kind IN ('personal','shared')", name="ck_calendars_kind")`;
  外键 `owner_user_id → users.id` `ondelete="CASCADE"`
- **索引**:`ix_calendars_kind`、`ix_calendars_owner_user_id`

### 表 `calendar_members`

- **位置**:`migrations/versions/0005_family_accounts.py:77`
- **列**:`id`(PK)、`calendar_id`、`user_id`、`role`、`created_at`、`updated_at`
- **约束**:`CheckConstraint("role IN ('owner','editor','viewer')", name="ck_calendar_members_role")`;
  外键 `calendar_id → calendars.id` 与 `user_id → users.id`,均 `ondelete="CASCADE"`;
  `UniqueConstraint("calendar_id","user_id", name="uq_calendar_members_pair")`
- **索引**:`ix_calendar_members_calendar_id`、`ix_calendar_members_user_id`

### 表 `invite_codes`

- **位置**:`migrations/versions/0005_family_accounts.py:99`
- **列**:`id`(PK)、`code_hash`、`label`、`max_uses`、`used_count`、`expires_at`、`enabled`、
  `created_by_user_id`、`created_at`、`updated_at`
- **索引**:`ix_invite_codes_code_hash`(唯一)
- **说明**:`created_by_user_id` **不建外键**,只记创建者 id

### 表 `feed_tokens`

- **位置**:`migrations/versions/0005_family_accounts.py:116`
- **列**:`id`(PK)、`user_id`、`label`、`token_hash`、`enabled`、`created_at`、`updated_at`、`last_used_at`
- **约束**:外键 `user_id → users.id` `ondelete="CASCADE"`,**可空**(`NULL` 即家庭共享范围)
- **索引**:`ix_feed_tokens_user_id`、`ix_feed_tokens_token_hash`(唯一)

## 变更

### `events`:新增 `calendar_id` 与 `created_by_user_id`

- **位置**:`migrations/versions/0005_family_accounts.py:133`(加列)、`:136`(建索引)
- **加列**:`calendar_id`、`created_by_user_id`,均 `nullable=True`、`String(36)`
- **索引**:`ix_events_calendar_id`、`ix_events_calendar_start(calendar_id, deleted_at, start_at_utc)`
- **注意(已实测)**:这两个加列**没有携带外键约束**——`batch_alter_table` 只加列,
  迁移里没有 `create_foreign_key`。跑完 0005 后 `sqlite_master` 里 `events` 没有任何
  `FOREIGN KEY` 行,而新表 `calendars` / `feed_tokens` 有。因此
  `app/models.py` 声明的 `ondelete="SET NULL"` 只对**按模型新建的库**
  (`Base.metadata.create_all`,测试库走这条路,且 `app/db.py` 打开了 `PRAGMA foreign_keys=ON`)
  由数据库执行;由本迁移升级上来的既有库里,`events.calendar_id` / `events.created_by_user_id` /
  `sync_recipients.user_id` / `agent_tokens.user_id` 只是普通的可空字符串列,
  删账号时不会有数据库级的自动置空。
- **为什么可空**:升级完成时还没有账号,必须先允许 `NULL`,再由引导回填;
  在引导跑完之前这些事件的 `calendar_id` 为空,不属于任何人的可见范围(语义见 `docs/static/app-models.md`)
- **应用层兜底(2026-10-08 补)**:因为升级库里没有外键,`app/accounts.py::delete_user`
  不再依赖 `ON DELETE SET NULL`,而是**显式**把该账号留下的共享事件
  `created_by_user_id = None`(个人日历连同其中的事件本来就由它显式删除),
  因此两条建库路径的删账号结果一致。**不要在删除路径上重新引入对外键的依赖**。

### `sync_recipients`:新增 `user_id`

- **位置**:`migrations/versions/0005_family_accounts.py:145`(加列)、`:147`(建索引)
- **加列**:`user_id`,`nullable=True`
- **索引**:`ix_sync_recipients_user_id`
- **语义**:为空即外部邮箱;非空即「这行属于某个账号」的**身份标记**
- **⚠️ 别人不要再用 `user_id` 当范围(2026-10-11 补)**:本迁移**只加 `user_id` 一列**,
  它在这之后被当成「收信范围」使用,直接引发了真机升级缺陷——引导把**每一个**历史外部收件人都写成
  `user_id = 首位管理员`,同一账号名下出现多行,第二次启动时 `scalar_one_or_none()` 抛
  `MultipleResultsFound`,容器重启循环。**收件人的范围现在归 `scope_user_id`**
  (由迁移 `0006_recipient_scope` 新增),统一由 `app/accounts.py::recipient_scope_user_id`
  解释为 `scope_user_id or user_id or None`;`user_id` 只保留身份语义(每个账号至多一行)。
  详见 `docs/static/migrations-0006.md` 与 `AGENTS.md` 不变量 21。
- **注意**:本迁移的加列同样**不携带外键**(`batch_alter_table` 只加列),删账号时的清理在应用层做

### `agent_tokens`:新增 `user_id` 与 `calendar_id`

- **位置**:`migrations/versions/0005_family_accounts.py:149`(加列)、`:152`(建索引)
- **加列**:`user_id`、`calendar_id`,均 `nullable=True`
- **索引**:`ix_agent_tokens_user_id`
- **语义**:`user_id` 为空的历史令牌由引导补成首位管理员;`calendar_id` 为空表示用归属账号的私人日历

## `downgrade()`

- **位置**:`migrations/versions/0005_family_accounts.py:156`
- **行为**:按相反顺序删索引、删列(`agent_tokens` → `sync_recipients` → `events`)与删表
  (`feed_tokens` → `invite_codes` → `calendar_members` → `calendars` → `users`)
- **说明**:这是**破坏性**操作——账号、日历、成员关系、邀请码与订阅令牌数据都会丢失;
  回退到单用户版本前必须先备份

## 为什么数据回填不写在迁移里

迁移只建结构、不回填数据,原因是回填需要运行时的账号凭据与配置,而这些在 Alembic 环境里拿不到:

1. **需要读环境变量**:首位管理员的账号名与口令(`ADMIN_USERNAME` / `ADMIN_PASSWORD(_HASH)`)
   由 `app/bootstrap.py::ensure_family_setup` 通过 `app/config.py` 的 `Settings` 读取;
   迁移脚本没有也不应构造 `Settings`。
2. **需要口令哈希工具**:首个账号要写 `password_hash`,以及 `must_change_password`
   (口令是首次启动自动生成还是注入),这属于应用启动逻辑。
3. **需要按当前模型做幂等补齐**:创建私人日历、家庭共享日历、成员关系、账号收件人,
   以及把历史事件/收件人/Agent 令牌挂到首位管理员,都要能重复执行且无副作用;
   迁移只在版本推进时执行一次,不适合承担这个职责。
4. **`alembic.ini` 的 URL 写死且不读 `DATA_DIR`**(见 `AGENTS.md`),手工跑迁移可能打到真实库;
   把数据变更留在启动引导里,可以让它和 `DATA_DIR` 指向同一个库。

**因此升级顺序是:迁移把列建出来(全部可空)→ 启动引导回填 →
之后 `Event.calendar_id` 不应再为空。** 相关测试:
`tests/test_family_accounts.py::test_legacy_events_are_moved_to_owner_personal_calendar`、
`::test_legacy_recipients_keep_owner_scope_on_first_upgrade`、
`::test_legacy_agent_token_is_bound_to_owner`、`::test_bootstrap_is_idempotent`。

## 变更记录

- 2026-10-08:新增本文档。归档迁移 0005 的五张新表、三张既有表的新增列与索引,
  以及「结构在迁移、回填在引导」的分工与回退的破坏性。
- 2026-10-11:在 `sync_recipients`:新增 `user_id` 一节补上**指向 0006 的说明**:
  0005 本身只加 `user_id` 一列,别人不要再用它当范围;并订正该节的加列/建索引行号
  (`:145`/`:147`)。