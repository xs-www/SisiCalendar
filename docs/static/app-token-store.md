# `app/token_store.py`

远程 Agent 接入令牌的生成与持久化管理。新增于 2026-09-29(平台内管理令牌)。

安全基调:**明文只返回一次,库里只存 `sha256`**,与 `AGENTS.md` 第 6 条对 SMTP 口令的处理同级。
数据表定义见 `app/models.py` 的 `AgentToken`,`app/models.py:479`。

家庭版(2026-10-08)起每枚令牌**带作用域**:绑定一个账号(`user_id`)与一条默认落库日历
(`calendar_id`),Agent 新建的日程按该账号的权限落库。同一套哈希与一次性明文约定也被
`app/feed_tokens.py`(ICS 订阅令牌)与 `app/invites.py`(邀请码)复用。

## `AgentToken`(表 `agent_tokens`)

- **位置**:`app/models.py:479`;迁移 `migrations/versions/0003_agent_tokens.py`
  (作用域列由 `migrations/versions/0005_family_accounts.py` 增加)
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `label`:给人看的名称,≤100 字符
  - `token_hash`:`sha256(明文)` 的 64 位十六进制,**唯一索引**
  - `user_id`:令牌归属账号;为空的历史令牌在启动引导时补成首位管理员
  - `calendar_id`:默认落库日历;为空表示用归属账号的个人日历
  - `enabled`:停用后立即失效但记录保留
  - `created_at` / `updated_at`:naive UTC
- **不变量**:表内**永不出现令牌明文**;`token_hash` 有唯一约束,同一枚令牌不会重复入库
- **不变量**:作用域只影响「用哪份权限」,不构成额外的授权来源——写入仍要过
  `app/accounts.py::CalendarAccess.can_write`

## `generate_token(prefix=TOKEN_PREFIX) -> str`

- **位置**:`app/token_store.py:28`
- **用途**:生成一枚新令牌明文
- **参数**:`prefix` 只用于人眼辨识(Agent 令牌 `sisi_`、订阅令牌 `sisi_feed_`、
  邀请码 `sisi_invite_`),**不参与鉴权与哈希**
- **返回**:`prefix` + `secrets.token_urlsafe(32)`(约 43 字符熵)
- **副作用**:无

## `hash_token(token) -> str`

- **位置**:`app/token_store.py:36`
- **用途**:明文 → `sha256` 十六进制摘要,鉴权与存储都用它
- **参数**:令牌明文
- **返回**:64 字符十六进制
- **副作用**:无
- **不变量**:令牌是高熵随机串,不加盐、不用慢哈希(与管理员口令的 Argon2id 区分开)

## `fingerprint_of_hash(token_hash) -> str`

- **位置**:`app/token_store.py:41`
- **用途**:界面展示与审计对账用的指纹
- **返回**:哈希前 16 位,与 `AgentPrincipal.fingerprint`(`app/deps.md`)完全一致,
  因此审计日志里的 `agent:<指纹>` 能直接对上是哪枚令牌
- **副作用**:无

## `create_token(session, label, *, user_id=None, calendar_id=None) -> tuple[str, AgentToken]`

- **位置**:`app/token_store.py:55`
- **用途**:新建令牌,返回一次性明文与记录
- **参数**:
  - `label`:去空白后不得为空、≤100 字符,否则抛 `SettingValidationError`
  - `user_id` / `calendar_id`(**本次新增**):决定这枚令牌的写入作用域;
    两者都为空表示「历史式」无归属令牌,由鉴权与引导兜底绑首位管理员
- **返回**:`(明文, AgentToken)`;明文**只在这里出现一次**,调用方必须立即交给用户
- **副作用**:`session.add` + `flush`(事务由调用方提交)
- **不变量**:明文不得写日志、不得回显在后续页面;`label` 与作用域之外不写别的字段

## `set_scope(session, token, *, user_id, calendar_id) -> None`

- **位置**:`app/token_store.py:86`
- **用途**(**本次新增**):改一枚既有令牌的归属账号与默认日历,不换明文、不换哈希
- **参数**:`user_id` / `calendar_id` 传 `None` 或空串都归一为「无」
- **副作用**:改字段并 `flush`;`updated_at` 刷新
- **不变量**:只改作用域,`token_hash` 与 `label` 不变,因此已持有明文的调用方不会被静默改权限
  ——**但**下次请求就会按新作用域判定
- **测试**:当前**没有**引用该函数的测试,也没有生产调用方(界面尚未接通)

## `list_tokens(session) -> list[AgentToken]`

- **位置**:`app/token_store.py:100`
- **用途**:管理界面渲染列表
- **返回**:含已停用令牌,按 `created_at.asc(), id.asc()` 排序;同一秒内创建的多枚也能稳定定序
- **副作用**:无

## `enabled_hashes(session) -> list[str]`

- **位置**:`app/token_store.py:112`
- **用途**:取出所有启用令牌的哈希
- **返回**:哈希字符串列表(不含明文)
- **副作用**:无
- **说明**:鉴权**不再**调用它——`app/deps.py::require_agent_token` 现在按行取整条记录,
  以便带出 `user_id`/`calendar_id`;本函数保留但当前无调用方

## `get_token` / `set_enabled` / `rename` / `delete`

- **位置**:`app/token_store.py:122` / `:126` / `:132` / `:138`
- **用途**:按 id 取记录、启停、改名、撤销(硬删除)
- **参数**:`rename` 的 `label` 校验规则同 `create_token`
- **副作用**:改字段并 `flush`;`delete` 直接删除记录
- **说明**:撤销是硬删除——令牌哈希随即消失,无需保留;`updated_at` 同步刷新

## 与 `.env` 的关系

`AGENT_API_TOKEN`(逗号分隔多个)**仍然生效**,与库内令牌取并集鉴权,作为兜底/恢复通道:

- 已有部署不必改动;
- 数据库损坏时仍可用 `.env` 里的令牌应急;
- `.env` 令牌归属首位管理员,`calendar_id` 为空(写进他的个人日历);
- 两处都没有令牌时,`/api/agent` 整体返回 404(见 [`app-deps.md`](app-deps.md))。

## 测试

`tests/test_agent_tokens.py`:一次性明文、库内只有哈希、DB 令牌鉴权、与 env 令牌并存、
撤销/停用立即生效、登录 + CSRF 守卫、明文不进日志。
作用域相关:`tests/test_family_accounts.py::test_agent_token_carries_scope`、
`::test_legacy_agent_token_is_bound_to_owner`。

## 变更记录

- 2026-09-29:新增。平台内可创建/改名/启停/撤销远程接入令牌;明文一次性,库内只存哈希;
  与 `AGENT_API_TOKEN` 并集鉴权。
- 2026-10-08:家庭版。`generate_token` 增加 `prefix` 参数;`create_token` 增加
  `user_id` / `calendar_id` 作用域;新增 `set_scope`;`AgentToken` 增加两列;
  `enabled_hashes` 不再是鉴权路径;并按当前源码校正全部行号(表定义 `app/models.py:479`)。