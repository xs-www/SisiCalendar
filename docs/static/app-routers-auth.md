# `app/routers/auth.py`

登录、登出、邀请码注册(`/login`、`/logout`、`/register`)与**邮箱验证**
(`/verify-email` 及其三个动作)。

家庭版起登录主体是 `users` 表里的账号(不再是环境变量里的单一管理员):

- `/login` 校验账号口令,会话里带 `user_id`;
- `/register` 用邀请码自助注册,**必须填邮箱**,注册成功即登录但账号处于**待验证**;
- 两个入口都有限流,注册额外受邀请码自身的次数与有效期约束;
- 未验证的普通成员进不了任何业务面,由 `app/deps.py::_gate_verification` 送到
  `/verify-email`(`AGENTS.md` 不变量 22);管理员豁免。

`ADMIN_USERNAME` / `ADMIN_PASSWORD(_HASH)` 只在首次启动创建首位管理员,
详见 `app/bootstrap.py::ensure_family_setup`。

所有站内跳转都用 `app/urls.py::url()`,因此从 `/calendar/login` 进来的请求一律跳回
`/calendar/...`(见 `docs/static/app-urls.md`、`tests/test_public_prefix.py`)。

## 常量与辅助

| 符号 | 位置 | 说明 |
|---|---|---|
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `app/routers/auth.py:55` / `:56` | 登录限流:8 次 / 300 秒,键 `login:<客户端 IP>` |
| `REGISTER_RATE_LIMIT` / `REGISTER_RATE_WINDOW_SECONDS` | `:57` / `:58` | 注册限流:5 次 / 600 秒,键 `register:<客户端 IP>` |
| `_client_key(request)` | `:61` | 限流分桶键;取不到对端时用 `"unknown"` |
| `_safe_next(value)` | `:66` | 只放行站内相对路径(必须以 `/` 开头且不是 `//`),否则回落到 **`urls.url("/admin")`**(带当前前缀),防开放重定向 |
| `_login_context(...)` / `_register_context(...)` | `:73` / `:221` | 渲染上下文;每次都**新签发**一枚登录 CSRF,并写进双提交 Cookie |
| `_set_login_cookies(response, settings, csrf_token)` | `:94` | 写 `sisi_login_csrf`(httponly、samesite=strict、max_age=3600) |
| `_start_session(response, settings, user_id, username)` | `:106` | 签发并写 `sisi_session`,同时删掉登录 CSRF Cookie |
| `_verify_context(...)` | `:430` | 验证页上下文:`current_user`、`csrf_token`、`email`、`resend_wait`、`code_length`、`ttl_minutes`、`max_attempts`、`notice`、`error`、`calendar_name`;`sent="1"`/`"0"` 分别补一条成功提示 / 失败提示 |

## 路由契约

### `GET /login` — `login_page`

- **位置**:`app/routers/auth.py:120`
- **参数**:`next`(站内跳回路径,经 `_safe_next`)、`notice`(一次性提示文案)
- **行为**:已登录(`optional_user` 非 `None`)时 **303 → `_safe_next(next)`**;否则渲染 `login.html`
  并写登录 CSRF Cookie
- **返回**:200 HTML / 303
- **副作用**:写 `sisi_login_csrf` Cookie
- **测试**:`tests/test_page_security.py::test_login_page_also_gets_csp`、
  `tests/test_family_web.py::test_logged_in_user_is_redirected_away_from_register`(注册页同款逻辑)、
  `tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`

### `POST /login` — `login_submit`

- **位置**:`app/routers/auth.py:138`
- **表单参数**:`username` / `password` / `csrf_token` / `next`
- **校验顺序**(**限流最先**):
  1. 限流超限 → **429**,带 `Retry-After`,审计 `login/rate_limited`
  2. 双提交 CSRF 不符 → **400**(「表单已过期,请重新提交。」),审计 `login/csrf_failed`
  3. 账号口令错 / 账号停用 → **401**(「用户名或口令不正确。」),审计 `login/denied`
  4. 成功 → **303**
- **成功行为**:`mark_login` 刷新 `last_login_at`、提交、**重置该 IP 的登录限流**;
  签发会话(带 `user_id`);审计 `login/success`
- **跳转目标(优先级从高到低)**:
  1. `must_change_password` → **`urls.url("/admin/me")+"?notice=请先修改初始口令"`**
     (忽略 `next`),即**先改初始口令再进管理界面**;
  2. `app/email_verify.py::needs_email_verification(user)` → **`urls.url("/verify-email")`**
     —— 未验证的账号登录后直接被送到验证页,不让他在别处碰一鼻子灰;
  3. 否则 → `_safe_next(next)`。
- **返回**:303(成功)/ HTML 错误页(400/401/429)
- **不变量**:错误文案不区分「用户名不存在」与「口令不对」;口令不写日志
- **测试**:`tests/test_family_web.py::test_initial_password_login_is_guided_to_change_it`
  (`must_change_password` 分支,断言 303 → `/admin/me` 且带 `notice`);
  `tests/conftest.py` 的登录夹具间接覆盖;**未验证账号登录后的验证页跳转没有专门测试**

### `POST /logout` — `logout`

- **位置**:`app/routers/auth.py:210`
- **参数**:依赖 `CsrfDep` —— **登出也要 CSRF**(表单或 `X-CSRF-Token`)
- **行为**:审计 `logout/success`,**303 → `urls.url("/login")`**(带当前前缀),
  删除 `sisi_session` Cookie
- **不变量**:未登录调用抛 `NotAuthenticated`;缺 CSRF 抛 `CsrfFailure`
- **测试**:`tests/conftest.py`(登出夹具);没有专门的登出缺 CSRF 回归测试

### `GET /register` — `register_page`

- **位置**:`app/routers/auth.py:245`
- **参数**:`code`(邀请码,预填进表单;不做校验,校验在提交时)
- **行为**:已登录 → **303 → `urls.url("/admin")`**;否则渲染 `register.html` 并写登录 CSRF Cookie
- **测试**:`tests/test_family_web.py::test_register_page_and_submit`、
  `::test_register_page_gets_csp`、
  `tests/test_family_pages.py::test_invite_plaintext_is_shown_once_and_register_link_works`

### `POST /register` — `register_submit`

- **位置**:`app/routers/auth.py:260`
- **表单参数**:`code` / `username` / `display_name` / `email` / `password` / `password_confirm` /
  `csrf_token`
- **校验顺序与状态码**(**限流最先**):
  1. 限流超限 → **429** + `Retry-After`,审计 `register/rate_limited`
  2. 双提交 CSRF 不符 → **400**,审计 `register/csrf_failed`
  3. 两次口令不一致 → **422**(「两次输入的口令不一致。」)
  4. **邮箱为空 → 422**(「请填写邮箱:注册后需要用它接收验证码和你的固定订阅地址。」)
     —— 邮箱是**必填**的,这一步在调用 `app/invites.py` 之前,校验位置就在本路由
  5. `app/invites.py::register_with_invite`(带 `settings=settings`)抛 `AccountError`
     (用户名/邮箱/口令等字段问题)→ **422**,回滚,审计 `register/invalid`,
     文案取各字段错误拼接
  6. 抛 `ValueError`(邀请码不存在/已用尽/已停用/已过期)→ **403**,回滚,审计 `register/denied`
  7. 其他未预期异常 → **500**,回滚,审计与响应都**不泄露内部细节**
  8. 成功 → **303 → `urls.url('/verify-email')?sent=1|0`**
- **成功行为**:
  - `effective_settings(session)` 取生效设置 → `email_verify.issue_code(...)` 登记验证码
    (库里只有 HMAC)→ `session.commit()`;
  - 重置该 IP 的注册限流,审计 `register/success`;
  - `email_verify.send_code_mail(...)` 发信;发出去则再记一条审计 `verify_code_sent/success`;
  - 签发会话(**注册成功即登录**),并以 `sent='1'`/`'0'` 告诉验证页「邮件发出去了没有」
    (`'0'` 时验证页给出「SMTP 未配置或发信失败,请点重新发送」)。
- **副作用**:消费邀请码使用次数、创建账号/私人日历/成员关系/**固定订阅地址**
  (`create_user(settings=...)`)、登记验证码、发验证码邮件
- **不变量**:邀请码核对只在 `app/invites.py`;本路由不做次数/有效期判断;
  **注册不会把账号直接置为可用**——它一定是待验证状态
- **测试**:`tests/test_email_verify.py::test_register_requires_email`(缺邮箱 422 且账号未创建)、
  `tests/test_family_web.py::test_register_page_and_submit`、
  `::test_register_rejects_bad_invite_code`(403)、
  `::test_register_rejects_password_mismatch`(422)、
  `::test_register_consumes_invite_once`(第二次 403)、
  `::test_register_is_rate_limited_per_client`、
  `::test_logged_in_user_is_redirected_away_from_register`

## 邮箱验证路由(2026-10-12 新增)

### `GET /verify-email` — `verify_email_page`

- **位置**:`app/routers/auth.py:459`;依赖 **`AnyUserDep`**(登录即可,**不过**验证闸门)
- **参数**:`sent`(取 `"1"`/`"0"`,由注册成功后的 303 带上)
- **行为**:`needs_email_verification(current.user)` 为假 → **303 → `urls.url("/admin")`**
  (已验证/管理员/无邮箱);否则渲染 `verify_email.html`
- **上下文**:`_verify_context(...)` —— `email`、`resend_wait`(距离可重发还差多少秒,
  模板据此把按钮置灰)、`code_length`(6)、`ttl_minutes`、`max_attempts`、`notice`、`error`
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`(验证页本身 200,
  页面里能看到邮箱)、`::test_unverified_admin_is_not_blocked`(管理员被重定向走)

### `POST /verify-email` — `verify_email_submit`

- **位置**:`app/routers/auth.py:476`;依赖 **`CsrfAnyDep`**;**表单**:`code`
- **行为**:`email_verify.check_code(...)` 返回非 `ok` 时 `commit()` 后按状态渲染 422
  (文案:`missing` 还没有可用验证码 / `expired` 已过期 / `too_many` 错误次数过多已作废 /
  `mismatch` 不正确),同时记审计 `verify_email/<status>`
- **验证通过(`ok`)**:
  1. `sync_user_recipient(session, current.user)` —— 这才建出账号收件人(未验证前不建);
  2. `account_subscription_url(session, settings, current.user)` 算出**固定**订阅地址;
  3. `commit()`,审计 `verify_email/success`;
  4. `email_verify.send_subscription_mail(...)` 把固定地址发给本人;成功再记一条
     审计 `subscription_mail_sent/success`;
  5. **303 → `urls.url("/admin/me")+"?notice=邮箱已验证,固定订阅地址已发到你的邮箱。"`**
- **不变量**:验证码明文只出现在邮件正文;库里只有 HMAC;审计 detail 不含验证码
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`
  (303 → `/admin/me`、第二封邮件含 `PUBLIC_BASE_URL/<固定令牌>.ics`、页面同样能看到该地址)、
  `::test_wrong_code_is_counted_and_then_invalidated`(4 次 422 且 `attempts` 递增,
  第 5 次作废后再填旧码提示「重新发送」)、`::test_expired_code_is_rejected`

### `POST /verify-email/resend` — `verify_email_resend`

- **位置**:`app/routers/auth.py:516`;依赖 **`CsrfAnyDep`**,无表单字段
- **行为**:
  - `needs_email_verification` 为假 → **303 → `urls.url("/admin")`**
  - `email_verify.resend_wait_seconds(...)` 大于 0 → **429**,
    文案「发送过于频繁,请 N 秒后再试。」
  - 否则 `issue_code`(换新码、清零尝试次数)→ `commit()` → `send_code_mail`;
    审计 `verify_code_sent/success` 或 `/failed`;
    返回 **200**(发出)/ **503**(没发出),正文按 `sent="1"`/`"0"` 给提示
- **不变量**:冷却窗口由 `settings.email_code_resend_seconds`(默认 60 秒)决定;
  **重发会立刻作废旧验证码**
- **测试**:`tests/test_email_verify.py::test_resend_cooldown_then_new_code`
  (冷却期内 429、冷却后 200、旧码失效、新码可用)

### `POST /verify-email/email` — `verify_email_change`

- **位置**:`app/routers/auth.py:547`;依赖 **`CsrfAnyDep`**;**表单**:`email`
- **用途**:注册时邮箱写错了的补救入口:改地址 → 重新发码,不必找管理员
- **行为**:
  - `needs_email_verification` 为假 → **303 → `urls.url("/admin")`**
  - `update_profile(session, current.user, email=email)`,抛 `AccountError` 或
    `SettingValidationError` → 回滚 + **422**;
  - `email_verify.mark_unverified(...)`(作废旧码)→ `sync_user_recipient(...)`
    (地址变了,收件人跟着变)→ `issue_code(...)` → `commit()`;
  - `send_code_mail(...)`;审计 `verify_email_retarget/success`;
    返回 **200**(发出)/ **503**(没发出),提示语里邮箱一律 `mask_email` 脱敏
- **不变量**:改邮箱走的是 `app/accounts.py::update_profile`,**邮箱唯一性**仍由它把关;
  换地址必然重新验证
- **测试**:`tests/test_email_verify.py::test_can_fix_a_mistyped_email_from_the_verify_page`

## 相关

- 邀请码本身的管理(创建/停用/列表)在页面路由 `/admin/partials/invites`,见
  `docs/static/app-routers-pages.md`;哈希存储与 `unusable_reason` 在 `app/invites.py`。
- 依赖语义(`NotAuthenticated` / `CsrfFailure` / `Forbidden` / `EmailNotVerified`、
  CSRF 依赖族)见 [`app-deps.md`](app-deps.md);会话字段与盐值见
  [`app-security.md`](app-security.md)。
- 验证码的生成/校验/冷却与固定地址邮件在 `app/email_verify.py`(另有文档),
  两封邮件的 MIME 构造在 `app/mailer.py`(**本仓库暂无对应静态文档**)。
- 资料页改邮箱时的「重新验证」链路见 `docs/static/app-routers-pages.md`
  (`save_profile_action`)。

## 找不到对应测试的点

- **未验证账号从 `/login` 登录后被送到 `/verify-email`** 这条分支没有专门测试
  (注册路径的验证页跳转有覆盖,但那是 `register_submit` 自己的 303)。
- `/login` 的 429 限流与 400 CSRF 分支没有专门测试(注册侧的状态码有覆盖)。
- `/logout` 的 CSRF 守卫没有专门测试。
- `/verify-email` 与 `/verify-email/email` 的 **503 分支**(SMTP 未配置或发信失败)
  没有专门测试;`sent="0"` 时注册后验证页的提示也没有单独断言。
- 「v1 会话升级后失效」没有回归测试,见 [`app-security.md`](app-security.md)。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档 `/login` 按账号认证与 `must_change_password` 跳转、
  `/logout` 走 `CsrfDep`、`/register` 邀请码注册(限流 / 自动登录 / 403·422·500 语义)。
- 2026-10-12:邮箱验证。`/register` 新增**必填邮箱**校验(422)与注册后
  **303 → `/verify-email?sent=0|1`**(登记验证码 + 发码 + 审计);
  `/login` 对未验证账号改送 `/verify-email`;新增四个验证路由
  (`GET/POST /verify-email`、`/verify-email/resend` 60 秒冷却 429、
  `/verify-email/email` 改地址重发),验证通过后补建账号收件人并邮件告知**固定**订阅地址;
  全部跳转改用 `urls.url()` 以跟随公开前缀;按当前源码校正全部行号。