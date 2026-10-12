# `app/routers/api_recipients.py`

邮件收件人配置 API(前缀 `/api/v1`,**仅家庭管理员**)。

家庭版里收件人的**身份**与**收信范围**是两件事(见 `docs/static/migrations-0006.md`):

- **账号收件人**(`sync_recipients.user_id` 非空):账号邮箱,在「我的设置 / 成员页」维护,
  只会收到**该账号可见日历**(个人 + 共享)里尚未结束的日程;
- **外部收件人**(`user_id` 为空):身份上没有登录能力;范围由 `scope_user_id` 决定——
  为空时只收到**家庭共享日历**范围(不含任何人的私人日程),非空时是单用户版升级上来的
  **历史绑定**(沿用某成员的可见范围,`scope = "member"`)。

因此新增与启用都需要显式 `acknowledge_full_calendar=true`(表示管理员已确认对方的收信范围)。
账号收件人**不能**通过本接口改名或删除,否则会和账号邮箱脱节——那类改动走账号资料
(`app/accounts.py::sync_user_recipient` 是唯一来源)。

## 辅助函数

### `serialize_recipient(recipient, *, session=None) -> dict`

- **位置**:`app/routers/api_recipients.py:40`
- **用途**:序列化收件人,并带上收信范围,前端才能区分三种范围
- **返回**:`id` / `email` / `display_name` / `enabled` / `created_at` / `updated_at` /
  `last_sent_at` / `masked_email`,外加:
  - `user_id`:`None` 表示外部收件人——**身份**标记,不是范围
  - `username`:给了 `session` 且是账号收件人时回查登录名,否则 `None`
  - `scope_user_id`:`app/accounts.py::recipient_scope_user_id(recipient)` 的结果
    (`scope_user_id or user_id or None`),即该行按谁的可见范围收信
  - `scope_user_name`:`scope_user_id` 对应账号的 `username`,查不到或为空时为 `None`
  - `scope`:三种取值(`app/routers/api_recipients.py:48`–`:54`)
    - `"account"`:`user_id` 非空,即账号收件人;
    - `"member"`:身份是外部邮箱、但 `scope_user_id` 指向某成员——单用户版升级上来的
      **历史绑定**,收信范围与升级前一致;
    - `"family"`:范围为空,只收家庭共享日历。
  - `scope_label`:与 `scope` 对应的中文文案,源码原文为
    `"本人可见日历(个人 + 家庭共享)"` / `f"{scope_user.label if scope_user else '某成员'}可见日历(历史绑定)"`
    / `"家庭共享日历"`
- **副作用**:给了 `session` 时按 `user_id` 与 `scope_user_id` 各查一次账号
- **不变量**:返回值不含明文之外的凭据;邮箱同时给出原文与 `masked_email`
  (`app/util.mask_email`);`scope` 必须由 `recipient_scope_user_id` 派生,
  不得自己拼 `user_id` 判断
- **测试**:`tests/test_family_pages.py::test_recipient_api_reports_scope_and_blocks_account_rows`
  (断言 `{"admin@example.test": "account", "guest@example.test": "family"}` 与
  `scope_label.startswith("本人可见日历")`)、
  `::test_recipient_partial_shows_scope`(页面侧);
  `"member"` 这一支与 `scope_user_id` / `scope_user_name` 两个字段**暂无直接测试**

### `_validate_email(raw) -> str`

- **位置**:`app/routers/api_recipients.py:73`
- **用途**:规范化(`normalize_email`,统一小写去空白)并校验邮箱格式
- **状态码**:**422** `validation_error`,字段名 `email`;空值与格式错误都走这里
- **返回**:规范化后的邮箱(≤320 字符)
- **测试**:`tests/test_api_settings.py::test_invalid_email_rejected`、
  `::test_create_and_list_recipient`(断言规范化结果)

### `_require_acknowledgement(payload, *, enabling) -> None`

- **位置**:`app/routers/api_recipients.py:90`
- **用途**:新增或**由停用转为启用**时必须显式确认收信范围
- **参数**:`enabling=True` 才强制;`acknowledge_full_calendar` 为真直接放行
- **状态码**:**422** `confirmation_required`,字段名 `acknowledge_full_calendar`
- **不变量**:只停用不需要确认;失败时不得写入任何字段
- **测试**:`tests/test_api_settings.py::test_requires_acknowledgement_before_adding`、
  `::test_disable_then_enable_requires_acknowledgement`

### `_reject_account_recipient(recipient, *, action) -> None`

- **位置**:`app/routers/api_recipients.py:104`
- **用途**:账号收件人由账号邮箱派生,不通过本接口维护
- **状态码**:**409** `conflict`,字段名 `user_id`;文案提示去成员页或该成员的「我的设置」操作
- **参数**:`action` 会拼进中文文案(如「修改邮箱」「删除收件人」)
- **测试**:`tests/test_family_pages.py::test_recipient_api_reports_scope_and_blocks_account_rows`
  (删除路径)

## 路由契约

| 路由 | 函数 | 位置 | 依赖 |
|---|---|---|---|
| `GET /api/v1/sync-recipients` | `list_recipients` | `app/routers/api_recipients.py:118` | `AdminDep` |
| `POST /api/v1/sync-recipients` | `create_recipient` | `:130` | `CsrfHeaderAdminDep` |
| `PATCH /api/v1/sync-recipients/{id}` | `update_recipient` | `:163` | `CsrfHeaderAdminDep` |
| `DELETE /api/v1/sync-recipients/{id}` | `delete_recipient` | `:204` | `CsrfHeaderAdminDep` |

### `list_recipients(admin, session) -> dict[str, object]`

- **返回**:`{"items": [...], "total": n, "enabled": n}`;按 `created_at.asc()` 排序
- **不变量**:成员调用 **403**;账号收件人与外部收件人**一起**列出,用 `scope` 区分
- **测试**:`tests/test_family_pages.py::test_member_cannot_use_admin_apis`、
  `tests/test_api_settings.py::test_create_and_list_recipient`

### `create_recipient(payload, admin, session)`

- **位置**:`app/routers/api_recipients.py:130`;请求体 `RecipientWrite`
  (`app/schemas.py:174`)
- **状态码**:**201** 成功;**403** 缺 CSRF 或非管理员;**409** 邮箱已存在;**422** 邮箱非法
  或缺 `acknowledge_full_calendar`
- **行为**:`enabled` 缺省按 `True`;`user_id` 一律写 `None`(**本接口只建外部收件人**);
  邮箱先规范化再查重(大小写不敏感)
- **副作用**:插入一行 `sync_recipients` 并提交;日志用 `mask_email` 脱敏
- **不变量**:不能通过本接口创建账号收件人——账号收件人由
  `app/accounts.py::sync_user_recipient` 派生
- **测试**:`tests/test_api_settings.py::test_requires_acknowledgement_before_adding`、
  `::test_duplicate_email_conflicts`、`::test_recipient_mutations_require_csrf`

### `update_recipient(recipient_id, payload, admin, session)`

- **位置**:`app/routers/api_recipients.py:163`
- **状态码**:**200** 成功;**404** 收件人不存在;**409** 目标是账号收件人(改邮箱)
  或新邮箱与他人冲突;**422** 邮箱非法 / 启用时缺确认
- **字段语义**:只处理显式提交的字段(`email is not None`、`display_name is not None`、
  `enabled is not None`);`display_name` 提交空串等价于清空
- **启用语义**:`enabled` 由 `False → True` 才要求 `acknowledge_full_calendar`;
  已经是启用状态再提交 `enabled=True` 不重复要求
- **不变量**:改邮箱只对**外部收件人**有效;账号邮箱必须走账号资料
- **测试**:`tests/test_api_settings.py::test_disable_then_enable_requires_acknowledgement`

### `delete_recipient(recipient_id, admin, session)`

- **位置**:`app/routers/api_recipients.py:204`
- **状态码**:**200** `{"ok": true, "id": ..., "masked_email": ...}`;**404** 不存在;
  **409** 账号收件人
- **副作用**:硬删除该行
- **不变量**:删除外部收件人**不影响**任何账号邮箱
- **测试**:`tests/test_api_settings.py::test_remove_recipient`、
  `tests/test_family_pages.py::test_recipient_api_reports_scope_and_blocks_account_rows`

## 三种范围与邮件的关系

- 收件人范围由 `app/accounts.py::recipient_scope_user_id` 决定:
  `scope_user_id or user_id or None`——有值 → 该账号可见范围;为空 → 家庭共享范围。
  `user_id` 只是身份标记,**不要单独拿它当范围**。
- 附件快照按收件人范围落盘,`(export_version, recipient_id)` 是幂等键;
  账号收件人在**清空账号邮箱**时会连同收件人记录一起删除
  (`app/accounts.py::sync_user_recipient`)。
- 删除账号时,`scope_user_id` 指向它的**外部**行会被退回家庭共享范围
  (`app/accounts.py::delete_user`)。
- **测试**:`tests/test_family_mail.py::test_external_recipient_only_gets_family_calendar`、
  `::test_account_recipient_gets_own_visible_scope`、`::test_clearing_email_stops_delivery`、
  `::test_account_and_external_recipients_coexist_and_are_idempotent`、
  `::test_legacy_external_recipient_keeps_owner_scope_attachment`
  (`"member"` 范围的附件落盘与正文标签);
  删账号时把 `scope_user_id` 退回家庭共享范围**暂无直接测试**。

## 找不到对应测试的点

- 「账号收件人**改名**被 409 拒绝」没有专门测试(删除路径有覆盖;
  见 `tests/test_family_pages.py::test_recipient_api_reports_scope_and_blocks_account_rows`)。
- 「`PATCH` 改邮箱撞到别人 → 409」没有专门测试(创建路径的重复邮箱有覆盖)。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档账号/外部两类收信范围、`scope`/`scope_label` 序列化、
  `acknowledge_full_calendar` 确认参数(新增与重新启用)、账号收件人 409 规则,
  以及四个接口的管理员 + CSRF 依赖。
- 2026-10-11:`serialize_recipient` 新增 `scope_user_id` / `scope_user_name`,`scope` 扩为
  `account` / `member`(历史绑定)/ `family` 三种,`scope_label` 文案更新;
  「两类范围」小节改名并改述为「三种范围」;顺带订正本文件其余条目的源码行号。