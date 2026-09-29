# `app/token_store.py`

远程 Agent 接入令牌的生成与持久化管理。新增于 2026-09-29(平台内管理令牌)。

安全基调:**明文只返回一次,库里只存 `sha256`**,与 `AGENTS.md` 第 6 条对 SMTP 口令的处理同级。
数据表定义见 `app/models.py` 的 `AgentToken`,`app/models.py:236`。

## `AgentToken`(表 `agent_tokens`)

- **位置**:`app/models.py:236`;迁移 `migrations/versions/0003_agent_tokens.py`
- **字段**:
  - `id`:`uuid4` 字符串主键
  - `label`:给人看的名称,≤100 字符
  - `token_hash`:`sha256(明文)` 的 64 位十六进制,**唯一索引**
  - `enabled`:停用后立即失效但记录保留
  - `created_at` / `updated_at`:naive UTC
- **不变量**:表内**永不出现令牌明文**;`token_hash` 有唯一约束,同一枚令牌不会重复入库

## `generate_token() -> str`

- **位置**:`app/token_store.py:26`
- **用途**:生成一枚新令牌明文
- **返回**:`sisi_` 前缀 + `secrets.token_urlsafe(32)`(约 43 字符熵)
- **副作用**:无
- **说明**:前缀只为人眼辨识,**不参与鉴权**

## `hash_token(token) -> str`

- **位置**:`app/token_store.py:31`
- **用途**:明文 → `sha256` 十六进制摘要,鉴权与存储都用它
- **参数**:令牌明文
- **返回**:64 字符十六进制
- **副作用**:无
- **不变量**:令牌是高熵随机串,不加盐、不用慢哈希(与管理员口令的 Argon2id 区分开)

## `fingerprint_of_hash(token_hash) -> str`

- **位置**:`app/token_store.py:36`
- **用途**:界面展示与审计对账用的指纹
- **返回**:哈希前 16 位,与 `AgentPrincipal.fingerprint`(`app/deps.py`)完全一致,
  因此审计日志里的 `agent:<指纹>` 能直接对上是哪枚令牌
- **副作用**:无

## `create_token(session, label) -> tuple[str, AgentToken]`

- **位置**:`app/token_store.py:52`
- **用途**:新建令牌,返回一次性明文与记录
- **参数**:`label` 去空白后不得为空、≤100 字符,否则抛 `SettingValidationError`
- **返回**:`(明文, AgentToken)`;明文**只在这里出现一次**,调用方必须立即交给用户
- **副作用**:`session.add` + `flush`(事务由调用方提交)
- **不变量**:明文不得写日志、不得回显在后续页面

## `list_tokens(session) -> list[AgentToken]`

- **位置**:`app/token_store.py:71`
- **用途**:管理界面渲染列表
- **返回**:含已停用令牌,按 `created_at.asc(), id.asc()` 排序;同一秒内创建的多枚也能稳定定序
- **副作用**:无

## `enabled_hashes(session) -> list[str]`

- **位置**:`app/token_store.py:82`
- **用途**:鉴权时取出所有启用令牌的哈希
- **返回**:哈希字符串列表(不含明文)
- **副作用**:无
- **调用方**:`app/deps.py::require_agent_token`,逐个 `hmac.compare_digest` 做常量时间比对

## `get_token` / `set_enabled` / `rename` / `delete`

- **位置**:`app/token_store.py:93` / `:97` / `:103` / `:109`
- **用途**:按 id 取记录、启停、改名、撤销(硬删除)
- **参数**:`rename` 的 `label` 校验规则同 `create_token`
- **副作用**:改字段并 `flush`;`delete` 直接删除记录
- **说明**:撤销是硬删除——令牌哈希随即消失,无需保留;`updated_at` 同步刷新

## 与 `.env` 的关系

`AGENT_API_TOKEN`(逗号分隔多个)**仍然生效**,与库内令牌取并集鉴权,作为兜底/恢复通道:

- 已有部署不必改动;
- 数据库损坏时仍可用 `.env` 里的令牌应急;
- 两处都没有令牌时,`/api/agent` 整体返回 404(见 `docs/static/app-deps.md`)。

## 测试

`tests/test_agent_tokens.py`:一次性明文、库内只有哈希、DB 令牌鉴权、与 env 令牌并存、
撤销/停用立即生效、登录 + CSRF 守卫、明文不进日志。

## 变更记录

- 2026-09-29:新增。平台内可创建/改名/启停/撤销远程接入令牌;明文一次性,库内只存哈希;
  与 `AGENT_API_TOKEN` 并集鉴权。