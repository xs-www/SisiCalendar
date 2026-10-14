# `app/routers/auth.py`

登录、登出、邀请码注册(`/login`、`/logout`、`/register`)、**邮箱验证**
(`/verify-email` 及其三个动作)与**初始口令救援**(`GET/POST /bootstrap`,2026-10-14 新增)。

家庭版起登录主体是 `users` 表里的账号(不再是环境变量里的单一管理员):

- `/login` 校验账号口令,会话里带 `user_id`;
- `/register` 用邀请码自助注册,**必须填邮箱**,注册成功即登录但账号处于**待验证**;
- 两个入口都有限流,注册额外受邀请码自身的次数与有效期约束;
- 未验证的普通成员进不了任何业务面,由 `app/deps.py::_gate_verification` 送到
  `/verify-email`(`AGENTS.md` 不变量 22);管理员豁免;
- `/bootstrap` 是**首位管理员**专用的救援入口:只在 `must_change_password` 为真时存在,
  用 `<DATA_DIR>/bootstrap_code` 里的引导码设一次新口令,成功后立即关闭
  (见 [`app-bootstrap-access.md`](app-bootstrap-access.md))。

`ADMIN_USERNAME` / `ADMIN_PASSWORD(_HASH)` 只在首次启动创建首位管理员,
详见 `app/bootstrap.py::ensure_family_setup`。

所有站内跳转都用 `app/urls.py::url()`,因此从 `/calendar/login` 进来的请求一律跳回
`/calendar/...`(见 `docs/static/app-urls.md`、`tests/test_public_prefix.py`)。

## 常量与辅助

| 符号 | 位置 | 说明 |
|---|---|---|
| `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` | `app/routers/auth.py:56` / `:57` | 登录限流:8 次 / 300 秒,键 `login:<客户端 IP>` |
| `REGISTER_RATE_LIMIT` / `REGISTER_RATE_WINDOW_SECONDS` | `:58` / `:59` | 注册限流:5 次 / 600 秒,键 `register:<客户端 IP>` |
| `BOOTSTRAP_RATE_LIMIT` / `BOOTSTRAP_RATE_WINDOW_SECONDS` | `:61` / `:62` | 引导码通道限流:5 次 / 600 秒,键 `bootstrap:<客户端 IP>`(2026-10-14 新增);引导码本身 24 字节随机,限流只是防扫 |
| `BOOTSTRAP_CLOSED_DETAIL` | `:615` | 通道关闭时 404 的 `detail` 文案:「引导通道已关闭:首位管理员的口令不是初始口令。忘记口令请在服务器上执行 `python -m app.cli reset-admin-password`。」 |
| `_client_key(request)` | `:65` | 限流分桶键;取不到对端时用 `"unknown"` |
| `_safe_next(value)` | `:70` | 只放行站内相对路径(必须以 `/` 开头且不是 `//`),否则回落到 **`urls.url("/admin")`**(带当前前缀),防开放重定向 |
| `_login_context(...)` / `_register_context(...)` | `:77` / `:225` | 渲染上下文;每次都**新签发**一枚登录 CSRF,并写进双提交 Cookie |
| `_set_login_cookies(response, settings, csrf_token)` | `:98` | 写 `sisi_login_csrf`(httponly、samesite=strict、max_age=3600) |
| `_start_session(response, settings, user_id, username)` | `:110` | 签发并写 `sisi_session`,同时删掉登录 CSRF Cookie |
| `_bootstrap_context(request, settings, *, code, error, notice)` | `:621` | 引导页上下文:`error`、`notice`、`code`(预填)、`login_csrf`、`cookie_name`、`cookie_value`、`calendar_name`、`code_file`(引导码文件路径,只给路径不给码);每次渲染都新签发一枚登录 CSRF |
| `_verify_context(...)` | `:434` | 验证页上下文:`current_user`、`csrf_token`、`email`、`resend_wait`、`code_length`、`ttl_minutes`、`max_attempts`、`notice`、`error`、`calendar_name`;`sent="1"`/`"0"` 分别补一条成功提示 / 失败提示 |

## 路由契约

### `GET /login` — `login_page`

- **位置**:`app/routers/auth.py:124`
- **参数**:`next`(站内跳回路径,经 `_safe_next`)、`notice`(一次性提示文案)
- **行为**:已登录(`optional_user` 非 `None`)时 **303 → `_safe_next(next)`**;否则渲染 `login.html`
  并写登录 CSRF Cookie
- **返回**:200 HTML / 303
- **副作用**:写 `sisi_login_csrf` Cookie
- **测试**:`tests/test_page_security.py::test_login_page_also_gets_csp`、
  `tests/test_family_web.py::test_logged_in_user_is_redirected_away_from_register`(注册页同款逻辑)、
  `tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`

### `POST /login` — `login_submit`

- **位置**:`app/routers/auth.py:142`
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

- **位置**:`app/routers/auth.py:214`
- **参数**:依赖 `CsrfDep` —— **登出也要 CSRF**(表单或 `X-CSRF-Token`)
- **行为**:审计 `logout/success`,**303 → `urls.url("/login")`**(带当前前缀),
  删除 `sisi_session` Cookie
- **不变量**:未登录调用抛 `NotAuthenticated`;缺 CSRF 抛 `CsrfFailure`
- **测试**:`tests/conftest.py`(登出夹具);没有专门的登出缺 CSRF 回归测试

### `GET /register` — `register_page`

- **位置**:`app/routers/auth.py:249`
- **参数**:`code`(邀请码,预填进表单;不做校验,校验在提交时)
- **行为**:已登录 → **303 → `urls.url("/admin")`**;否则渲染 `register.html` 并写登录 CSRF Cookie
- **测试**:`tests/test_family_web.py::test_register_page_and_submit`、
  `::test_register_page_gets_csp`、
  `tests/test_family_pages.py::test_invite_plaintext_is_shown_once_and_register_link_works`

### `POST /register` — `register_submit`

- **位置**:`app/routers/auth.py:264`
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

- **位置**:`app/routers/auth.py:463`;依赖 **`AnyUserDep`**(登录即可,**不过**验证闸门)
- **参数**:`sent`(取 `"1"`/`"0"`,由注册成功后的 303 带上)
- **行为**:`needs_email_verification(current.user)` 为假 → **303 → `urls.url("/admin")`**
  (已验证/管理员/无邮箱);否则渲染 `verify_email.html`
- **上下文**:`_verify_context(...)` —— `email`、`resend_wait`(距离可重发还差多少秒,
  模板据此把按钮置灰)、`code_length`(6)、`ttl_minutes`、`max_attempts`、`notice`、`error`
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`(验证页本身 200,
  页面里能看到邮箱)、`::test_unverified_admin_is_not_blocked`(管理员被重定向走)

### `POST /verify-email` — `verify_email_submit`

- **位置**:`app/routers/auth.py:480`;依赖 **`CsrfAnyDep`**;**表单**:`code`
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

- **位置**:`app/routers/auth.py:520`;依赖 **`CsrfAnyDep`**,无表单字段
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

- **位置**:`app/routers/auth.py:551`;依赖 **`CsrfAnyDep`**;**表单**:`email`
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

## 初始口令救援路由(2026-10-14 新增)

> 领域层契约(`ensure_code` / `verify_code` / `pending_owner` / `clear_code` 等)与三条不变量见
> [`app-bootstrap-access.md`](app-bootstrap-access.md);模板见
> [`templates.md`](templates.md) 的 `templates/bootstrap.html` 一节。

两条路由都 `include_in_schema=False`(公开文档里不出现),都按 `app/urls.py` 的机制挂**两遍**
(裸路径 `/bootstrap` 与带前缀的 `/calendar/bootstrap` 都可用),判定依据一律是
**`app/bootstrap_access.py::pending_owner(session)` 是否为空**——不看来源地址,
也不另设开关。

### `GET /bootstrap` — `bootstrap_page`

- **位置**:`app/routers/auth.py:643`
- **参数**:`code`(查询串里的引导码,预填进表单;为空则表单留空)
- **行为**:
  1. `effective_settings(session)` 取生效设置(`:651`);
  2. `pending_owner(session) is None`(口令不是初始口令,或库里没有账号)→
     抛 `HTTPException(404, detail=BOOTSTRAP_CLOSED_DETAIL)`(`:652`–`:653`)。
     **响应体是 FastAPI 默认的 `{"detail": …}` JSON**,仓库里没有为 404 注册处理器
     (只注册了 `NotAuthenticated` / `EmailNotVerified` / `CsrfFailure` / `Forbidden`,
     见 `app/main.py:173`–`:220`);
  3. 否则先 `bootstrap_access.ensure_code(resolved)` **自愈**(`:655`):文件被手工删掉时
     在这一步补回来,所以「能打开这一页」就等于「通道可用」;
  4. 渲染 `bootstrap.html`,上下文由 `_bootstrap_context`(`:621`)给出,
     并 `_set_login_cookies(response, resolved, login_csrf)`(`:658`)
     写登录 CSRF 双提交 Cookie。
- **返回**:200 HTML(带严格 CSP——路径集合见 [`app-urls.md`](app-urls.md) 的
  `is_admin_path`)/ 404
- **副作用**:**可能创建** `<DATA_DIR>/bootstrap_code`(自愈);写 `sisi_login_csrf`
  Cookie;页面里出现引导码文件路径(`code_file`),但**不出现也不记日志**引导码本身
- **不变量**:通道关闭时**必须**404,不得渲染表单、不得自动开通道
  (即「打开页面」不能反过来把已关闭的通道打开);`GET` **不消费**引导码
- **测试**:`tests/test_bootstrap_access.py::test_bootstrap_page_is_closed_when_the_password_is_not_initial`
  (裸路径与 `/calendar/bootstrap` 都是 404)、
  `::test_bootstrap_page_serves_the_form_with_csp`(200、带
  `Content-Security-Policy`、URL 上的码被预填、带前缀入口同样 200)

### `POST /bootstrap` — `bootstrap_submit`

- **位置**:`app/routers/auth.py:662`
- **表单参数**:`code` / `password` / `password_confirm` / `csrf_token`
- **校验顺序**(限流与 CSRF 都排在**通道判定之前**):
  1. **限流最先**:`rate_limiter.check(...)`(`app/routers/auth.py:684`–`:688`),
     键是 `bootstrap:<客户端 IP>`,`limit=BOOTSTRAP_RATE_LIMIT`、
     `window_seconds=BOOTSTRAP_RATE_WINDOW_SECONDS`;超限 → **429**,
     响应头带 `Retry-After`,审计 `bootstrap/rate_limited`;
  2. **登录 CSRF 双击校验**(`:695`–`:698`):比较 Cookie `sisi_login_csrf` 与表单
     `csrf_token`(`verify_login_csrf`)——**不是**依赖族的 `CsrfDep`/`CsrfAnyDep`,
     而是与登录/注册页同一套「双提交 + 服务端签名」;不符 → **400**
     「表单已过期,请重新提交。」,审计 `bootstrap/csrf_failed`;
  3. **通道判定**(`:700`–`:702`):`pending_owner(session) is None` → **404**
     (`BOOTSTRAP_CLOSED_DETAIL`,同上);
  4. **引导码**(`:704`–`:708`):`verify_code(resolved, code)` 为假 → **401**
     「引导码不正确:请把引导码文件里的那一串整行复制过来(区分大小写)。」,
     审计 `bootstrap/denied`;码**不消费、不轮换**,通道继续开着;
  5. **两次口令不一致**(`:710`–`:711`)→ **422**「两次输入的口令不一致。」;
  6. `set_password(session, owner, password, must_change=False)`(`:713`–`:717`):
     抛 `AccountError`(口令不合规等)→ **`session.rollback()`** + **422**
     (文案取各字段错误拼接);这一步**先于**任何提交,失败不会改库;
  7. **成功**(`:719`–`:725`):`session.commit()`(状态位落库)→
     `bootstrap_access.clear_code(resolved)`(**删掉引导码文件**)→
     `rate_limiter.reset(f"bootstrap:{client}")` → 审计 `bootstrap/success`
     (actor 是账号名,不是 IP)→ **303 → `<前缀>/login?notice=管理员口令已设置,请用它登录。`**
- **返回**:303(成功)/ HTML 表单(400 / 401 / 422,`bootstrap.html` + 错误文案)/
  429(带 `Retry-After`)/ 404
- **副作用**:写 `users.password_hash` / `must_change_password=False` / `updated_at`;
  删除 `<DATA_DIR>/bootstrap_code`;重置该 IP 的引导限流;写四条审计之一
  (`rate_limited` / `csrf_failed` / `denied` / `success`)
- **不变量**:
  - **成功后通道必须关上**:`must_change_password` 清零(由 `set_password`)、
    文件被删(由 `clear_code`),此后 `GET/POST /bootstrap` 立刻 404,
    旧引导码永久失效——这是「不是常驻后门」的落地点;
  - **失败绝不能关通道**:401(码错)与 422(口令不一致/不合规)都不得删文件、
    不得改状态位;422 必须 `rollback()`,不能把半截写入留在会话里;
  - 限流键是 `bootstrap:<客户端 IP>`,`_client_key` 取不到对端时用 `"unknown"`
    (Docker NAT 之下所有人看起来同源,限流只是防扫,见 `AGENTS.md` 部署陷阱 3);
  - 引导码明文与口令明文都**不得**进日志;审计 detail 里没有码,只有客户端或账号名;
  - **只有 `owner`**(首位管理员)会被改:`pending_owner` 已经限定,
    不要扩展成「任意带 `must_change_password` 的账号」。
- **测试**:`tests/test_bootstrap_access.py::test_setting_the_password_closes_the_channel`
  (303 → `/login`、文件被删、页面 404、`must_change_password` 清零、新口令能登录且
  不再被引导去改密)、
  `::test_wrong_code_and_bad_password_keep_the_channel_open`
  (错码 401 → 口令不一致 422 → 口令过短 422,全程码仍在)、
  `::test_bootstrap_rejects_a_missing_csrf_token`(**400** 且码仍在)、
  `::test_bootstrap_is_rate_limited_per_client`(连错 5 次后第 6 次 **429** + `Retry-After`)、
  `::test_closed_channel_rejects_the_post_too`(通道关闭后 POST 也 **404**)

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
- **`POST /bootstrap` 收到非 ASCII 引导码会 500**:`app/bootstrap_access.py::verify_code`
  直接调 `hmac.compare_digest(str, str)`,含非 ASCII 字符的候选值会抛 `TypeError`
  (实测报错文案是「comparing strings with non-ASCII characters is not supported」),
  路由没有捕获它,表现为 500 而不是 401。没有测试覆盖,见
  [`app-bootstrap-access.md`](app-bootstrap-access.md) 的「已知边界」。
- `GET /bootstrap` 的 **404 响应体形状**没有断言(现有用例只断言状态码;
  实际是 FastAPI 默认的 `{"detail": …}` JSON,不是纯文本)。
- `bootstrap` 限流的**窗口过期**(600 秒后重新放行)与「成功时重置限流」
  (`rate_limiter.reset`)没有专门用例,只有「连错 5 次 → 429」这一条。
- 成功后的 **303 目标只断言了 `/login` 前缀**,`?notice=管理员口令已设置,请用它登录。`
  的文案与 URL 编码没有被逐字断言。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档 `/login` 按账号认证与 `must_change_password` 跳转、
  `/logout` 走 `CsrfDep`、`/register` 邀请码注册(限流 / 自动登录 / 403·422·500 语义)。
- 2026-10-12:邮箱验证。`/register` 新增**必填邮箱**校验(422)与注册后
  **303 → `/verify-email?sent=0|1`**(登记验证码 + 发码 + 审计);
  `/login` 对未验证账号改送 `/verify-email`;新增四个验证路由
  (`GET/POST /verify-email`、`/verify-email/resend` 60 秒冷却 429、
  `/verify-email/email` 改地址重发),验证通过后补建账号收件人并邮件告知**固定**订阅地址;
  全部跳转改用 `urls.url()` 以跟随公开前缀;按当前源码校正全部行号。
- 2026-10-14:初始口令救援通道。新增 `GET /bootstrap`(`bootstrap_page`,通道关闭时 404、
  打开页面时 `ensure_code` 自愈、写登录 CSRF Cookie)与 `POST /bootstrap`
  (`bootstrap_submit`,限流 5 次 / 600 秒 → 429、登录 CSRF 双击校验 → 400、
  通道判定 → 404、码错 → 401、口令不一致或不合规 → 422、
  成功则 `set_password(must_change=False)` → `commit` → `clear_code` → 303 到
  `/login?notice=…`),以及新常量 `BOOTSTRAP_RATE_LIMIT` / `BOOTSTRAP_RATE_WINDOW_SECONDS` /
  `BOOTSTRAP_CLOSED_DETAIL` 与新辅助 `_bootstrap_context`;
  两条路由都 `include_in_schema=False`;按当前源码校正全文行号(本次新增的常量块让
  其后的行号整体 +4),并补「找不到对应测试的点」四条。