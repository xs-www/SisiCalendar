# `app/security.py`

认证、会话、CSRF 与限流。本次变更:家庭版会话载荷升级(盐值升到 **v2**)与
`create_session` / `read_session` 的字段要求。

管理入口虽然只绑定回环地址,仍按文档第 11 节作为第二层保护:Argon2id 口令哈希、
签名会话 Cookie、双提交 CSRF 令牌,以及对敏感动作的速率限制。

家庭版起登录主体是 `users` 表里的账号:`ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH`
只在**首次启动**用来创建首位管理员,不再是长期登录凭据。

## 变更(2026-10-08)

### `SESSION_SALT = "sisi-calendar-session-v2"`

- **位置**:`app/security.py:40`
- **用途**:签名会话 Cookie 的 itsdangerous salt
- **本次变更**:由 `sisi-calendar-session-v1` 升到 **v2**
- **不变量**:换 salt 后 **v1 会话全部失效**,用户需要重新登录——这是**预期行为**,
  不是 bug:家庭版会话多了 `uid`,半升级的旧 Cookie 只带用户名,无法安全地映射到账号
- **影响面**:升级后第一次访问管理页会跳 `/login`;订阅地址(`/calendar/...`)不依赖会话,
  不受影响

### `SessionData(user_id, username, csrf_token, issued_at)`

- **位置**:`app/security.py:137`(frozen dataclass)
- **本次变更**:新增 `user_id`(账号主键);`username` 仍是展示用登录名
- **字段语义**:
  - `user_id`:每次请求回表查账号,账号删除/停用即会话失效
  - `csrf_token`:`secrets.token_urlsafe(32)`,与 Cookie 同签名,不落库
  - `issued_at`:签发时间(epoch 秒),只作记录

### `create_session(*, user_id, username, settings=None) -> tuple[str, SessionData]`

- **位置**:`app/security.py:144`
- **用途**:签发一枚签名会话 Cookie 值
- **参数**(**全部为关键字参数**):
  - `user_id`:非空字符串,账号主键;缺失无法签发
  - `username`:登录名,写进载荷的 `sub`
  - `settings`:不传用 `get_settings()`;签名密钥来自 `settings.load_or_create_session_secret()`
- **返回**:`(cookie 值, SessionData)`
- **载荷**:`{"uid": user_id, "sub": username, "csrf": csrf_token, "iat": issued_at}`
- **副作用**:无(只签名;写 Cookie 由路由做,见 `app/routers/auth.py::_start_session`)
- **测试**:`tests/conftest.py` 的登录夹具间接覆盖;
  账号维度见 `tests/test_family_accounts.py::test_disabled_account_cannot_authenticate`

### `read_session(token, settings=None) -> SessionData | None`

- **位置**:`app/security.py:161`
- **用途**:校验签名与有效期(`settings.session_max_age_seconds`),还原会话
- **字段要求**(**本次变更,三者缺一即返回 `None`**):`uid` / `sub` / `csrf`
  都必须是 `str`;`iat` 缺失按 `0` 处理
- **返回**:`SessionData`;签名错误、过期、载荷不是 dict、任一必需字段缺失或类型不对
  一律返回 `None`(调用方按未登录处理)——**不抛异常**
- **不变量**:`read_session` 只回答「这枚 Cookie 是不是我们签发的、字段齐不齐」,
  **不查库、不判角色**;账号是否存在、是否启用由 `app/deps.py::_load_current` 决定
- **不变量**:任何失败路径都不得把载荷内容写进日志

### `verify_csrf(session, provided) -> bool`

- **位置**:`app/security.py:188`
- **用途**:常量时间比较会话里的 `csrf` 与调用方提交值
- **返回**:`session` 为 `None` 或 `provided` 为空时 `False`
- **不变量**:比较必须用 `hmac.compare_digest`,不得用 `==`

## 其余既有契约(供检索)

| 符号 | 位置 | 一句话 |
|---|---|---|
| `SESSION_COOKIE_NAME` / `LOGIN_CSRF_COOKIE_NAME` | `app/security.py:34` / `:35` | `sisi_session` 与登录前双提交 Cookie 名 |
| `CSRF_HEADER_NAME` / `CSRF_FORM_FIELD` | `:36` / `:37` | `X-CSRF-Token` 与 `csrf_token` |
| `LOGIN_CSRF_SALT` | `:41` | 登录前 CSRF 的 salt(未改,仍 v1) |
| `hash_password` / `verify_password` / `needs_rehash` | `:49` / `:53` / `:60` | Argon2id 口令 |
| `ensure_admin_credential` | `:71` | 首次启动生成 `admin.hash`;返回的一次性明文只打印一次 |
| `load_admin_hash` | `:100` | 读管理员口令哈希 |
| `authenticate` | `:112` | **仅排错用**:校验 `.env` 管理员口令;家庭版登录走 `app/accounts.py::authenticate_user` |
| `create_login_csrf` / `read_login_csrf` / `verify_login_csrf` | `:201` / `:206` / `:216` | 登录/注册表单的双提交 CSRF(有效期 3600 秒) |
| `RateLimiter.check` / `reset` | `:235` / `:248` | 进程内滑动窗口;路由用它做限流 |
| `AuditLog.record` | `:263` | 只记动作与结果;**绝不记口令、会话、令牌明文与邮件正文** |

## 不变量(改动时必须保持)

- 会话 Cookie 是 **httponly + samesite=strict**,不参与任何 JS 逻辑。
- CSRF 令牌只存在于签名会话与页面渲染上下文里,不写库。
- 口令只以 Argon2id 哈希存在;会话与令牌明文只以签名/哈希形式存在。
- 换 salt / 改载荷结构必须同时确认「旧会话失效是可接受的」,并同步本文件与
  `docs/static/app-deps.md`。

## 测试

- 会话/登录:`tests/conftest.py`(登录夹具)、`tests/test_api_events.py::test_requires_login_for_event_api`
- 鉴权边界:`tests/test_api_agent.py::test_session_cookie_does_not_authorize_agent_endpoint`
- 安全响应头(含登录页):`tests/test_page_security.py::test_login_page_also_gets_csp`、
  `::test_common_security_headers`
- **注意**:v1 → v2 的「旧会话失效」没有专门的回归测试;现有测试都从新会话开始。

## 变更记录

- 2026-10-08:家庭版。`SESSION_SALT` 升到 `sisi-calendar-session-v2`(旧会话一律失效);
  `SessionData` 增加 `user_id`;`create_session` 改为关键字参数并签发 `uid`;
  `read_session` 要求 `uid`/`sub`/`csrf` 三者齐备;`authenticate` 降级为排错用。