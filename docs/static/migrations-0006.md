# `migrations/versions/0006_recipient_scope.py`

本迁移把邮件收件人的**身份**与**范围**拆开:给 `sync_recipients` 新增可空的
`scope_user_id` 列与索引 `ix_sync_recipients_scope_user_id`,把账号收件人的范围回填为
`scope_user_id = user_id`,并修复 `0005` 之后的启动引导制造出来的重复绑定。
数据步骤只在迁移执行到 `0006` 那一次发生,之后的日常维护仍由启动引导负责
(见 `docs/static/app-bootstrap.md`)。

## 背景(真机 Docker 升级发现的缺陷)

`0005_family_accounts` 只加了 `sync_recipients.user_id` 一列,而它后面的启动引导在
**首次升级**时把**每一个**历史外部收件人都写成 `user_id = 首位管理员`,于是同一个账号名下
挤进多行「账号收件人」。第二次启动时 `app/accounts.py::sync_user_recipient` 用
`scalar_one_or_none()` 取该账号名下的行,直接抛 `MultipleResultsFound`,
应用启动失败、容器进入重启循环(真机升级时实际发生)。

拆开之后:

- `user_id` = **身份**:非空表示这行是该账号的账号收件人(由 `users.email` 派生,**每账号一行**);
- `scope_user_id` = **范围**:发给该邮箱的附件按哪个账号的可见日历裁剪;为空 = 家庭共享日历。

范围统一由 `app/accounts.py::recipient_scope_user_id` 解释为
`scope_user_id or user_id or None`(见 `docs/static/app-accounts.md`)。

## 元信息

- **Revision ID**:`0006_recipient_scope`(`migrations/versions/0006_recipient_scope.py:37`)
- **Revises**:`0005_family_accounts`(`migrations/versions/0006_recipient_scope.py:38`)
- **Create Date**:2026-10-11
- **入口**:`upgrade()` 在 `migrations/versions/0006_recipient_scope.py:80`,
  `downgrade()` 在 `migrations/versions/0006_recipient_scope.py:95`
- **测试**:`tests/test_family_upgrade_recipients.py::test_migration_repair_splits_duplicate_bindings`、
  `::test_migration_repair_demotes_everything_when_account_has_no_email`
  ——测试库由 `Base.metadata.create_all` 建出、**不执行迁移**,所以这两个用例按文件路径
  动态导入迁移模块后单独调用 `_repair_duplicate_bindings`(见该文件 `:27`–`:37`)

## 新增

### 列 `sync_recipients.scope_user_id`

- **位置**:`migrations/versions/0006_recipient_scope.py:82`(加列)、
  `:83`(建索引 `ix_sync_recipients_scope_user_id`)
- **类型**:`String(36)`,`nullable=True`
- **语义**:该收件人收到的附件按哪个账号的可见范围裁剪;`NULL` = 家庭共享日历
- **注意**:**`batch_alter_table` 只加列,不携带外键**(与 0005 的加列完全一致)。
  模型层声明的 `ForeignKey("users.id", ondelete="SET NULL")` 只对按模型新建的库
  (`Base.metadata.create_all`,测试库走这条路)生效;由迁移升级上来的库里这一列只是
  普通的可空字符串列。因此删账号时的范围退回必须由
  `app/accounts.py::delete_user` 在应用层显式完成(见 `docs/static/migrations-0005.md`
  的「已实测」一节)
- **不变量**:`scope_user_id` 只表示范围,**不得**再被当成身份标记;
  「一个账号一行账号收件人」由 `user_id` 保证(见 `AGENTS.md` 不变量 21)

## 函数

### `upgrade() -> None`

- **位置**:`migrations/versions/0006_recipient_scope.py:80`
- **用途**:加列加索引,再回填范围并修复 `0005` 遗留的重复绑定
- **参数**:无(Alembic 从环境取连接,内部 `op.get_bind()` 拿 `sa.Connection`)
- **返回**:`None`
- **副作用**:`batch_alter_table` 加列与建索引;两条/多条 `UPDATE sync_recipients`
- **数据步骤**:
  1. `:87`–`:91`:账号收件人(原本 `user_id` 非空)的范围就是它所属的账号 →
     `UPDATE sync_recipients SET scope_user_id = user_id WHERE user_id IS NOT NULL`;
  2. `:92`:调 `_repair_duplicate_bindings(connection)` 修复重复绑定(规则见下)。
- **不变量**:升级后**任何** `user_id` 非空的账号名下至多一行;
  被降级的行仍保留原有收信范围(`scope_user_id = 该账号`),行为与升级前一致
- **测试**:`tests/test_family_accounts.py::test_legacy_recipients_keep_owner_scope_on_first_upgrade`
  改为断言 `recipient.scope_user_id == owner.id`(引导侧);迁移脚本本身的加列、建索引与
  两条回填 SQL **没有自动化测试**,只有修复函数被
  `tests/test_family_upgrade_recipients.py` 直接调用覆盖

### `_repair_duplicate_bindings(connection) -> None`

- **位置**:`migrations/versions/0006_recipient_scope.py:47`
- **用途**:把挤在同一个账号名下的多行收件人拆成「一行账号收件人 + 若干范围绑定」
- **参数**:`connection` 是 `op.get_bind()` 给出的 `sa.Connection`
  (全程用 `sa.text` 执行原生 SQL,不依赖 ORM 映射)
- **规则**:
  - 只处理 `user_id` 非空、且**同一个 `user_id` 有多行**的账号(`:49`–`:55`);
  - 有账号邮箱时,保留**邮箱与该账号 `users.email` 一致**的那一行(`:65`–`:70`),
    比较前用 `_normalize` 去空白并转小写;候选行按 `created_at, id` 排序(`:61`)取第一条匹配;
  - 其余行 `scope_user_id = 该账号, user_id = NULL`(`:73`–`:75`),
    即降级成「沿用某账号范围的外部邮箱」,**收信范围不变**;
  - 账号本身**没有邮箱**(或邮箱为空白)时 `keeper is None`,该账号名下**全部**行降级
    (SQL 条件 `(:keeper IS NULL OR id <> :keeper)`)。
- **返回**:`None`
- **副作用**:对 `sync_recipients` 批量 `UPDATE`(每个重复账号一条语句)
- **不变量**:绝不删除行(只改 `user_id` / `scope_user_id`),历史收件人的邮箱、
  `created_at`、`last_sent_at` 全部保留;修复后每个 `user_id` 至多一行,
  且任一行的收信范围都不变
- **测试**:`tests/test_family_upgrade_recipients.py::test_migration_repair_splits_duplicate_bindings`
  (保留邮箱与账号一致的行,`old@example.test` 的 `user_id` 置空、`scope_user_id` 仍为该账号)、
  `::test_migration_repair_demotes_everything_when_account_has_no_email`
  (账号无邮箱时全部降级)

### `_normalize(value) -> str`

- **位置**:`migrations/versions/0006_recipient_scope.py:43`
- **用途**:邮箱比较前的归一化:`None`/空串 → 空串,去首尾空白并转小写
- **参数**:任意字符串或 `None`
- **返回**:归一化后的字符串
- **副作用**:无
- **不变量**:邮箱比较必须走 `_normalize`(去首尾空白 + 转小写),不要直接比字符串,
  否则大小写或空白不一致的账号邮箱会被当成「没有匹配行」,导致该账号名下**全部**行被降级
- **测试**:暂无直接测试(两个修复用例的邮箱都已是小写规范形式,
  大小写/空白差异未被断言)

## `downgrade()`

### `downgrade() -> None`

- **位置**:`migrations/versions/0006_recipient_scope.py:95`
- **行为**:先把范围折回身份——`UPDATE sync_recipients SET user_id = scope_user_id
  WHERE user_id IS NULL AND scope_user_id IS NOT NULL`(`:97`–`:102`,
  **只在该行 `user_id` 为空时**),再删索引 `ix_sync_recipients_scope_user_id`
  与列 `scope_user_id`(`:103`–`:105`)
- **参数**:无
- **返回**:`None`
- **副作用**:`UPDATE sync_recipients` + `DROP INDEX` + `DROP COLUMN`
- **破坏性说明**:折回后同一个账号名下**可能重新出现多行** `user_id` 相同的收件人——
  那正是 `0005` 的原始缺陷状态。因此折回**不是无损的**:
  - 再升级回 `0006` 时,`_repair_duplicate_bindings` 会按**当时**的 `users.email`
    重新挑选 keeper,结果不一定与折回前一致;
  - 折回本身也**不保证**应用第二次启动不崩:旧引导/旧取行逻辑依赖
    `scalar_one_or_none()` 的唯一性。
  回退前先备份(`app/cli.py` 的备份命令)。
- **不变量**:折回只填 `user_id` 为空的那些行,不覆盖已有 `user_id`
- **测试**:暂无直接测试

## 没有测试覆盖的点

- `upgrade()` / `downgrade()` 的加列、建索引、删索引、删列语句没有自动化测试:
  测试库由 `Base.metadata.create_all` 建出,不执行迁移;
  `tests/test_family_upgrade_recipients.py` 只直接调用了 `_repair_duplicate_bindings`。
- `downgrade()` 折回 `user_id` 之后「同一账号又出现多行」这一破坏性后果没有自动化测试。
- `_normalize` 的大小写/首尾空白归一化没有专门用例。

## 变更记录

- 2026-10-11:新增本文档。归档 0006 的背景(真机 Docker 升级把每个历史收件人都写成
  `user_id = 首位管理员` → 同一账号多行 → 第二次启动 `MultipleResultsFound`)、
  `scope_user_id` 的加列与索引、账号收件人的范围回填、`_repair_duplicate_bindings`
  的重复行修复规则(保留邮箱与 `users.email` 一致的行、其余降级且范围不变、账号无邮箱时全部降级),
  以及 `downgrade()` 折回范围时的破坏性。