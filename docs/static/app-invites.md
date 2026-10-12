# `app/invites.py`

本模块实现邀请码:家庭成员自助注册的唯一入口——管理员生成、成员用码注册,库内只存哈希,
次数用尽/停用/过期后不可再用。

安全基调与远程接入令牌一致(**明文只显示一次,库里只存 `sha256`**,见 `AGENTS.md` 第 18 条)。
数据表定义见 `docs/static/app-models.md` 的 `InviteCode`;注册路由另有限流,
防止有人拿一枚有效邀请码去暴力猜用户名。

本轮(2026-10-12)的变化只有两处:注册**必须填邮箱**(校验在路由里),
`register_with_invite` 多了一个 `settings` 参数,把「登记固定订阅地址」这件事
转交给 `app/accounts.py::create_user`。

## 新增

### 常量

- **位置**:`app/invites.py:24`–`app/invites.py:28`
- **取值与含义**:
  - `INVITE_TOKEN_PREFIX = "sisi_invite_"`:明文前缀,**只为人眼辨识,不参与鉴权**;
  - `MAX_LABEL = 100`:邀请码名称长度上限;
  - `MAX_USES = 20`:`max_uses` 的上限;
  - `INVITE_PARAM = "code"`:注册页与邀请链接里的参数名(`/register?code=...`)。
- **不变量**:前缀字符串本身不是秘密,不得把它当作校验条件;
  邀请链接的形态由 `INVITE_PARAM` 决定,前端与文案都引用它。
  **`app/urls.py::invite_url()` 用的是自己那份 `INVITE_QUERY = "code"`
  (`app/urls.py:103`)**——两处是同一约定,改参数名必须同时改这两处,
  否则页面提示的链接与注册页读的参数会对不上

### `create_invite(session, *, label, max_uses=1, expires_at=None, created_by_user_id=None) -> tuple[str, InviteCode]`

- **位置**:`app/invites.py:39`
- **用途**:生成一张邀请码,返回一次性明文与记录
- **参数**:
  - `label` 去空白后不得为空、≤`MAX_LABEL`,否则抛 `SettingValidationError`
  - `max_uses` 必须是整数且落在 **1..`MAX_USES`(20)**,否则抛 `SettingValidationError`
    (非整数提示「必须是整数」,越界提示区间)
  - `expires_at`:naive UTC 或 `None`(**`None` 表示不过期**)
  - `created_by_user_id`:创建者账号 id,只记 id、不建外键
- **返回**:`(明文, InviteCode)`;明文形如 `sisi_invite_<43 字符>`,
  **只在这里出现一次**,调用方必须立即交给用户
- **副作用**:`session.add` + `flush`(事务由调用方提交);明文的哈希写 `code_hash`
- **不变量**:明文不得写日志、不得写入审计 detail、不得在后续页面回显;
  `used_count` 从 0 起、`enabled` 从 `True` 起
- **测试**:`tests/test_family_accounts.py::test_invite_registration_creates_member`、
  `::test_invite_is_stored_hashed_and_looked_up_by_hash`、`::test_invite_max_uses_bounds`、
  `tests/test_family_pages.py::test_invite_creation_validates_inputs`、
  `::test_invite_plaintext_is_shown_once_and_register_link_works`

### `list_invites(session) -> list[InviteCode]`

- **位置**:`app/invites.py:74`
- **用途**:管理面渲染邀请码列表
- **返回**:全部邀请码(含已停用/已用尽),按 `created_at.asc(), id.asc()` 排序
- **副作用**:无
- **不变量**:列表里**只有哈希与指纹**,永不包含明文
- **测试**:`tests/test_family_pages.py::test_invite_list_marks_used_up_invites`

### `get_invite(session, invite_id) -> InviteCode | None`

- **位置**:`app/invites.py:82`
- **用途**:按 id 取邀请码(启停/撤销前先取)
- **返回**:`InviteCode` 或 `None`
- **副作用**:无
- **测试**:暂无直接测试(由页面的启停/撤销入口调用)

### `find_by_code(session, code) -> InviteCode | None`

- **位置**:`app/invites.py:86`
- **用途**:按**明文**查邀请码:先 `sha256`,再按 `code_hash` 查唯一索引
- **参数**:`code` 会去空白;空串直接返回 `None`
- **返回**:命中的 `InviteCode` 或 `None`
- **副作用**:无
- **不变量**:查询只走哈希列,明文绝不入库、不比对明文;
  **是否存在、是否可用是两件事**——本函数不过滤状态
- **测试**:`tests/test_family_accounts.py::test_invite_is_stored_hashed_and_looked_up_by_hash`

### `unusable_reason(invite, *, now=None) -> str | None`

- **位置**:`app/invites.py:96`
- **用途**:判定邀请码是否可用,返回可直接展示给注册者的中文原因
- **参数**:`now` 便于测试注入时间;默认取 `utcnow()`
- **返回**:可用时 `None`;否则返回原因文案
- **判定顺序(必须保持)**:
  1. `invite is None` → 「邀请码无效,请向家庭管理员确认。」
  2. `remaining_uses <= 0` → 「该邀请码的使用次数已用完……」
  3. `not invite.enabled` → 「该邀请码已被停用……」
  4. `expires_at is not None and expires_at <= now` → 「该邀请码已过期……」
- **不变量**:**先判「次数用完」再判「已停用」**——用完的邀请码会被 `consume` 自动停用,
  这时告诉对方「次数已用完」比「已停用」更有指导意义;判定顺序改变会让文案与
  `tests/test_family_web.py::test_register_consumes_invite_once` 的断言失配
- **测试**:`tests/test_family_accounts.py::test_invite_respects_max_uses`、
  `::test_disabled_and_expired_invites_are_rejected`、
  `tests/test_family_web.py::test_register_rejects_bad_invite_code`、
  `::test_register_consumes_invite_once`

### `consume(session, invite) -> None`

- **位置**:`app/invites.py:114`
- **用途**:消耗一次使用次数
- **行为**:`used_count += 1`;若 `remaining_uses <= 0` 则同时置 `enabled = False`
  (一次性邀请码用完即停用);刷新 `updated_at`;`flush`
- **返回**:`None`
- **副作用**:更新 `invite_codes` 一行
- **不变量**:`used_count` 只增;**不提交事务**——由调用方与建账号处于同一事务,
  注册失败时计数必须一起回滚(路由在两处失败分支都 `session.rollback()`)
- **测试**:`tests/test_family_accounts.py::test_invite_registration_creates_member`
  (注册后 `used_count == 1` 且 `enabled is False`)、
  `tests/test_family_pages.py::test_invite_list_marks_used_up_invites`

### `set_enabled(session, invite, enabled) -> None`

- **位置**:`app/invites.py:122`
- **用途**:管理员停用/重新启用邀请码
- **副作用**:写 `enabled`、`updated_at` 并 `flush`
- **不变量**:停用不改 `used_count`;已用尽的邀请码即使重新启用也仍然不可用
  (`unusable_reason` 先判次数)
- **测试**:`tests/test_family_accounts.py::test_disabled_and_expired_invites_are_rejected`

### `delete(session, invite) -> None`

- **位置**:`app/invites.py:128`
- **用途**:撤销邀请码(**硬删除**)
- **副作用**:删除 `invite_codes` 一行并 `flush`
- **不变量**:撤销后明文再也无法注册;不需要保留记录(与令牌撤销同级处理)
- **测试**:暂无直接测试(由页面撤销入口调用)

### `register_with_invite(session, *, code, username, password, display_name=None, email=None, settings=None) -> User`

- **位置**:`app/invites.py:133`
- **用途**:用邀请码注册成员账号,并消耗一次使用次数
- **参数**:
  - `email`:**注册必填**——但这条校验**不在这里**,而在调用方
    `app/routers/auth.py::register_submit`(缺邮箱直接 422,连 `create_user` 都不会调)。
    本函数自己仍按 `create_user` 的规则处理:给了邮箱就严格校验,不给也能建号
    (引导与测试会这样建号)
  - `settings`(2026-10-12 新增):透传给 `create_user`(`settings=settings`,
    `app/invites.py:162`),让新账号在建号时就登记**固定订阅地址**;注册路由一定传,
    漏传则由启动引导兜底补上
- **校验顺序**:
  1. `find_by_code` 取记录 → `unusable_reason` 判定,**不可用即抛 `ValueError(原因文案)`**
     (路由把它渲染成 403 并提示用户,不消耗任何次数);
  2. 调 `app/accounts.py::create_user` 建账号:邀请码注册的账号**角色固定为 `member`**,
     字段错误原样抛 `AccountError`(**不消耗次数**,路由转 422);
  3. 只有账号建成功后才 `consume`(计数 +1,用尽则自动停用)。
- **返回**:新 `User`(已 `flush`;**调用方负责提交事务**)
- **自动开通**:账号创建即获得私人日历、加入所有共享日历(`viewer`)、
  有邮箱时建账号收件人、登记固定订阅地址——这几件事由 `create_user` 完成,
  本函数不重复实现
- **副作用**:写 `users`/`calendars`/`calendar_members`/可能的 `sync_recipients`/`feed_tokens`
  + 更新 `invite_codes`
- **不变量**:「建账号」与「消耗次数」必须同生共死:任一步失败都不得留下
  半个账号或凭空少一次次数;注册不授予管理员角色;
  **新账号默认是「待验证」状态**(注册后跳到验证页),这不影响本函数的原子性
- **测试**:`tests/test_family_accounts.py::test_invite_registration_creates_member`、
  `::test_invite_respects_max_uses`、`::test_disabled_and_expired_invites_are_rejected`、
  `tests/test_family_web.py::test_register_page_and_submit`(注册成功即登录且是普通成员)、
  `::test_register_consumes_invite_once`(同一枚码第二次被拒 403 且提示「用完」)、
  `tests/test_email_verify.py::test_register_requires_email`(缺邮箱 422、不消耗次数、
  不建账号)、`::test_full_verification_flow_mails_the_fixed_address`
  (注册建的号一注册就带着自己的固定地址)

## 变更

本模块是 2026-10 家庭版改造中**新增**的文件,邀请码是这次改造才出现的注册入口,
因此没有「旧函数改签名」的条目。它带来的既有模块变化:

- `app/routers/auth.py`:新增 `GET/POST /register`(带来源地址限流),
  注册失败按类型分流——`AccountError` → 422、邀请码不可用 → 403,
  两条失败路径都 `session.rollback()`。
  **2026-10-12 起注册必须填邮箱**——`register_submit` 的校验顺序是
  口令不一致 422 → **邮箱为空 422**(「请填写邮箱:注册后需要用它接收验证码和你的固定订阅地址。」)
  → 调 `register_with_invite(..., settings=settings)`,
  成功后再 303 到 `urls.url("/verify-email")`(见 `docs/static/app-routers-auth.md`);
- `app/accounts.py::create_user`:成为注册的建账号入口,负责私人日历、共享日历 `viewer`
  成员关系、账号收件人与**固定订阅地址**的自动开通(见 `docs/static/app-accounts.md`);
- `app/urls.py::invite_url()`:邀请链接的绝对地址由它拼(`<公开基地址>/register?code=<明文>`),
  与页面列表里提示的 `invite_base_url` 前缀必须一致(见 `docs/static/app-urls.md`);
- `app/routers/pages.py`:管理员面的邀请码列表/创建/启停/撤销;
- 数据模型:`invite_codes` 由迁移 0005 建出(见 `docs/static/migrations-0005.md`)。

## 变更记录

- 2026-10-08:新增本文档。归档邀请码的明文一次性、`sha256` 落库、`max_uses` 边界、
  有效期与 `unusable_reason` 的判定顺序(先次数、再停用、最后过期)、`consume` 的同事务原子性,
  以及 `register_with_invite` 的校验顺序与自动开通。
- 2026-10-12:`register_with_invite` 新增 `settings=None` 参数并透传给 `create_user`
  (注册即登记账号的固定订阅地址);写明「注册必须填邮箱」的校验其实在
  `app/routers/auth.py::register_submit`,以及本函数「不给邮箱也能建号」的边界;
  `INVITE_PARAM` 与 `app/urls.py::INVITE_QUERY` 的一致性要求补进常量条目;
  全文行号按当前源码校正(`list_invites`/`get_invite`/`find_by_code`/`unusable_reason`/
  `consume`/`set_enabled`/`delete`/`register_with_invite` 各后移一行,常量区间为 24–28)。