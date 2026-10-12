# `app/email_verify.py`

邮箱验证:6 位验证码 + 验证通过后补发「你的固定订阅地址」。它落实 `AGENTS.md`
不变量 22:非管理员账号**有邮箱但没验证**时进不了任何业务页面与接口,也不能成为邮件收件人;
首位管理员豁免(本机救急不能被自己的邮箱锁在门外)。

## 状态机

一个账号的验证状态只有三种,由 `users` 的五个列决定(`docs/static/migrations-0007.md`):

| 状态 | 判据 | 后果 |
|---|---|---|
| 不需要验证 | 管理员,或**没有邮箱** | 照常使用;界面持续提醒补填邮箱 |
| 待验证 | 非管理员 + 有邮箱 + `email_verified_at IS NULL` | 页面 303 → `/verify-email`;`/api` 403 `email_not_verified`;不进收件人 |
| 已验证 | `email_verified_at IS NOT NULL` | 正常使用;可成为账号收件人 |

验证码本身的生命周期:

```text
注册/重发/改邮箱 → issue_code()      登记哈希、TTL、清零尝试次数、记 sent_at
        ↓
  check_code(code)
        ├─ 没有哈希或没有过期时间              → "missing"
        ├─ 过期时间 <= now                     → "expired"(不改库)
        ├─ 尝试次数已达上限                    → "too_many"(不改库)
        ├─ 哈希不匹配                          → 次数 +1;达到上限就作废 → "mismatch"
        └─ 哈希匹配                            → 置 email_verified_at、清空哈希与次数 → "ok"
```

## 安全要点(改动时必须保持)

- 库里**只存** `HMAC(session_secret, f"verify:{user_id}:{code}")`,不存明文;
  验证码只有 6 位,没有服务端密钥就还原不出来。
- **验证码绝不进日志、不进审计 detail**:审计只记事件名与结果
  (`verify_code_sent` / `verify_email` / `verify_email_retarget`),
  发信失败只记 `type(exc).__name__`。
- 每次重发都换新码并把尝试次数清零,旧码**立即失效**;
- 每次错误都计数,**第 5 次错误直接把验证码作废**(必须重新发送);
- 验证码与邮箱绑定:**改了邮箱就重新发、重新验**;
- `data/session_secret` 轮换会让所有验证码哈希失效——密钥即身份,这是刻意的取舍
  (同 `AGENTS.md` 不变量 23 的固定订阅地址)。

## 常量

### `CODE_LENGTH` / `CODE_DIGITS`

- **位置**:`app/email_verify.py:43`–`app/email_verify.py:44`
- **取值与含义**:`CODE_LENGTH = 6`(验证码位数,注册页与验证页的 `maxlength` 引用它);
  `CODE_DIGITS = "0123456789"`
- **不变量**:验证码**允许前导零**(`"012345"` 是合法码),因此全链路一律按**字符串**
  比较与展示,任何地方都不得先转成整数

## 函数

### `generate_code() -> str`

- **位置**:`app/email_verify.py:47`
- **用途**:生成 6 位数字验证码
- **返回**:`CODE_LENGTH` 位十进制字符串,可能带前导零
- **副作用**:无(用 `secrets.choice`,不写库)
- **不变量**:必须用 `secrets` 的密码学随机源,不得换成 `random`
- **测试**:暂无直接测试(经
  `tests/test_email_verify.py::test_unverified_member_is_blocked` 的验证码邮件正文间接覆盖)

### `code_hash(settings, *, user_id, code) -> str`

- **位置**:`app/email_verify.py:52`
- **用途**:算验证码的 HMAC(库内唯一保存的形态)
- **参数**:`code` 会 `strip()`;密钥来自 `settings.load_or_create_session_secret()`
- **返回**:`HMAC-SHA256(服务端密钥, "verify:<user_id>:<code>")` 的 hex 字符串
- **副作用**:可能触发 `load_or_create_session_secret()` 创建 `data/session_secret`
  (首次运行)
- **不变量**:待校验的载荷格式 `verify:<user_id>:<code>` **不得改动**——改了会让所有
  在途验证码失效;明文 `code` 不得写日志;验证必须用 `hmac.compare_digest` 常量时间比对
- **测试**:暂无直接测试(由 `check_code` 的正/误码用例间接覆盖)

### `needs_email_verification(user) -> bool`

- **位置**:`app/email_verify.py:59`
- **用途**:这个账号是否被邮箱验证挡住(闸门的唯一判据)
- **豁免规则(必须保持)**:
  1. `user.is_admin` → `False`(**管理员豁免**,本机救急入口);
  2. 邮箱为空或纯空白(`(user.email or "").strip()` 为空)→ `False`
     (升级上来的老账号可能没填,只由界面持续提醒,不锁死);
  3. 其余情况下 `user.email_verified_at is None` → `True`
- **返回**:`True` = 需要验证(被挡住);`False` = 不受阻
- **副作用**:无
- **不变量**:豁免规则的第 2 条与 `app/accounts.py::sync_user_recipient` 的取件判据
  (**有邮箱且已验证**)是一对:「没邮箱不挡」只是因为没邮箱本来就不发信,
  不是因为没邮箱算验证过;判定**不得**依赖会话、请求或时间
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`、
  `::test_unverified_admin_is_not_blocked`(管理员豁免)
- **调用方**:`app/deps.py:146`(登录后的统一闸门,抛 `EmailNotVerified`)、
  `app/routers/auth.py:202`(登录后跳转判定)、`:467`、`:523`、`:556`(验证页自身的三处)、
  `app/routers/pages.py:419`

### `is_verified(user) -> bool`

- **位置**:`app/email_verify.py:72`
- **用途**:邮箱是否**已经**验证过(不问角色、不看有没有邮箱)
- **返回**:`user.email_verified_at is not None`
- **副作用**:无
- **不变量**:这里**没有**管理员豁免——管理员只要邮箱未验证,也不是收件人;
  与 `needs_email_verification` 的分工:前者是「能不能发信给这个地址」,
  后者是「要不要把这个人挡在门外」
- **测试**:`tests/test_email_verify.py::test_unverified_account_never_becomes_a_mail_recipient`、
  `::test_unverified_admin_is_not_blocked`(管理员未验证时名下没有收件人)
- **调用方**:`app/accounts.py:711`(`sync_user_recipient` 的取件判据)

### `mark_unverified(session, user) -> None`

- **位置**:`app/email_verify.py:76`
- **用途**:邮箱变更后重新进入待验证状态,旧验证码一并作废
- **行为**:`email_verified_at = None`、清空 `email_verify_code_hash` /
  `email_verify_expires_at` / `email_verify_sent_at`,`attempts = 0`,刷新 `updated_at`,再 `flush`
- **返回**:`None`
- **副作用**:写 `users` 一行(**不提交事务,由调用方提交**)
- **不变量**:只改状态,**不**发信(发信由调用方紧接着 `issue_code` +
  `send_code_mail` 完成);清空 `sent_at` 意味着改邮箱后能**立刻**重发,
  不受上一封的 60 秒冷却限制
- **测试**:`tests/test_email_verify.py::test_verified_member_who_changes_email_must_verify_again`
  (换邮箱后 `email_verified_at is None`)、
  `::test_can_fix_a_mistyped_email_from_the_verify_page`、
  `tests/test_family_pages.py::test_profile_update_starts_email_verification`
  (改邮箱后不建收件人,`SyncRecipient` 表为空)
- **调用方**:`app/routers/auth.py:583`(`/verify-email/email`)、
  `app/routers/pages.py:509`(资料页保存时邮箱变了)

### `issue_code(session, settings, user, *, now=None) -> str`

- **位置**:`app/email_verify.py:87`
- **用途**:生成并登记一枚新验证码,返回明文(只用于发信)
- **参数**:`now` 便于测试注入时间,默认 `utcnow()`;有效期 = `max(60, settings.email_code_ttl_seconds)`
  (默认 1800 秒 = 30 分钟)
- **返回**:验证码明文(6 位字符串)
- **副作用**:写 `user.email_verify_code_hash` / `email_verify_expires_at` /
  `email_verify_attempts`(清零)/ `email_verify_sent_at` / `updated_at`,再 `flush`
  (**不提交事务**)
- **不变量**:明文**只**返回给调用方去发信,**绝不**写日志、写审计、
  写库或回显页面;重发必须换新码(旧码立即失效,靠覆盖哈希实现)
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`
  (注册后立刻有验证码邮件)、`::test_wrong_code_is_counted_and_then_invalidated`
  (5 次错误后 `email_verify_code_hash is None`)、
  `::test_resend_cooldown_then_new_code`(重发得到不同的新码,旧码随即失效)、
  `tests/test_family_pages.py::test_profile_update_starts_email_verification`
  (改邮箱后 `email_verify_code_hash` 非空)
- **调用方**:`app/routers/auth.py:412`(注册)、`:535`(重发)、`:585`(改邮箱)、
  `app/routers/pages.py:510`(资料页改邮箱)

### `resend_wait_seconds(user, settings, *, now=None) -> int`

- **位置**:`app/email_verify.py:107`
- **用途**:距离可以再次发送还差多少秒
- **参数**:冷却读 `max(0, settings.email_code_resend_seconds)`(默认 60 秒)
- **返回**:
  - 从未发过(`email_verify_sent_at is None`)→ `0`;
  - 冷却配成 `0` → `0`(等于不限流);
  - 其余 → `max(0, int(冷却 - 已过秒数))`,`0` 表示**现在就能发**
- **副作用**:无
- **不变量**:只按库里记的 `sent_at` 判定,**不**用内存计时器或请求来源限流;
  返回 `int` 截断(不足 1 秒按 0 处理)
- **测试**:`tests/test_email_verify.py::test_resend_cooldown_then_new_code`
  (刚注册完点重发 → 429 且提示「频繁」;把 `sent_at` 推到 5 分钟前就能重发)
- **调用方**:`app/routers/auth.py:439`(验证页上下文)、`:525`(重发入口的 429 判定)

### `check_code(session, settings, user, code, *, now=None) -> str`

- **位置**:`app/email_verify.py:121`
- **用途**:校验验证码(状态机的核心),成功时置 `email_verified_at`
- **参数**:`code` 为 `None`/空串时按空串算哈希(必然不匹配);`now` 便于测试注入时间;
  上限读 `max(1, settings.email_code_max_attempts)`(默认 5)
- **返回**:下表五种状态之一(调用方按它给出中文提示)

  | 返回值 | 触发条件 | 是否改库 |
  |---|---|---|
  | `"missing"` | 没有 `email_verify_code_hash` 或没有 `email_verify_expires_at` | 否 |
  | `"expired"` | `email_verify_expires_at <= now` | 否 |
  | `"too_many"` | 已记尝试次数 ≥ 上限,而验证码记录还在 | 否 |
  | `"mismatch"` | 哈希不匹配(`hmac.compare_digest` 为假) | 次数 +1;达到上限时把哈希与过期时间**一并清空** |
  | `"ok"` | 哈希匹配 | 置 `email_verified_at = now`,清空哈希/过期时间,次数清零 |

- **副作用**:写 `users`(成功或失败都会动列)并 `flush`;**不提交事务**
- **不变量**:
  - 比对必须走 `hmac.compare_digest`,不得用 `==`;
  - 「第 N 次错误即作废」靠 `mismatch` 分支里 `>= 上限` 时清空哈希实现——
    清空后下一次调用落到 `missing`,所以**默认配置下 `too_many` 走不到**
    (它覆盖的是把 `EMAIL_CODE_MAX_ATTEMPTS` 调小、或库里尝试次数被外部改大的情形);
  - 成功时保留 `email_verify_sent_at`(供维护观测),验证状态本身只看 `email_verified_at`;
  - 本函数**不**发信、**不**建收件人,两件事由路由在 `ok` 之后做。
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`
  (`ok` → 303 到 `/admin/me`)、`::test_wrong_code_is_counted_and_then_invalidated`
  (`mismatch` 计数 1..4;第 5 次清空哈希,再用正确码 → 422「重新发送」)、
  `::test_expired_code_is_rejected`(`expired`)、
  `::test_resend_cooldown_then_new_code`(重发后旧码 → 422)
- **调用方**:`app/routers/auth.py:484`(`POST /verify-email`)

### `send_code_mail(settings, user, code) -> bool`

- **位置**:`app/email_verify.py:165`
- **用途**:发**第一封信**——验证码邮件
- **行为**:经 `app/mailer.py::send_verification_code`,正文含验证码、
  `ttl_minutes = max(1, email_code_ttl_seconds // 60)` 与验证页地址
  `{public_base_url}/verify-email`,主题 `[<日历名>] 邮箱验证码`
- **返回**:`True` = 已交给 SMTP;`False` = **SMTP 未配置或账号没有邮箱**(记一条
  warning 日志),或发送抛 `MailError`(**只记 `type(exc).__name__`**)——失败不抛给调用方
- **副作用**:发信(每位收件人单独发送);日志只记失败类型
- **不变量**:**验证码与验证页地址绝不出现在日志或审计里**——日志只允许出现失败类型;
  失败时验证码**已经登记**(`issue_code` 在发信之前),调用方提示用户「重新发送」即可;
  非 `MailError` 的异常(例如模板/编码错误)会照常抛出,不被吞掉
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`
  (注册后收件人是本人、主题含「验证码」)、
  `::test_can_fix_a_mistyped_email_from_the_verify_page`(第二封发到新地址)、
  `::test_verified_member_who_changes_email_must_verify_again`(`outbox[-1]` 是新地址)
- **调用方**:`app/routers/auth.py:416`(注册)、`:537`(重发)、`:587`(改邮箱)、
  `app/routers/pages.py:513`(资料页改邮箱)

### `send_subscription_mail(settings, user, *, subscription_url) -> bool`

- **位置**:`app/email_verify.py:188`
- **用途**:发**第二封信**——把该账号的**固定订阅地址**告知本人(验证通过后补发)
- **参数**:`subscription_url` 是**关键字参数**,由调用方用
  `app/feed_tokens.py::account_subscription_url` 现算(服务端可随时重算,不依赖「只显示一次」的明文)
- **返回**:`True` = 已交给 SMTP;`False` = SMTP 未配置 / 没有邮箱 / `MailError`
- **副作用**:发信;主题 `[<日历名>] 你的固定订阅地址`,正文含地址与「地址等同于密码」的提醒
- **不变量**:正文里的地址**只能**是该账号自己那条固定地址,不得出现别人的地址或
  并集地址;明文地址只允许出现在**邮件正文**里,不得写日志与审计
  (`AGENTS.md` 不变量 16、23)
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`
  (第二封信主题含「固定订阅地址」,正文里的地址等于
  `f"{public_base_url}/{account_feed_token(settings, member.id)}.ics"`,
  且同一条地址在「我的订阅地址」片段里也看得到)
- **调用方**:`app/routers/auth.py:506`(`POST /verify-email` 成功分支,
  先 `sync_user_recipient` 再发信)

## 与其它模块的接口

- **闸门**:`app/deps.py:146` 在解析出当前用户后调 `needs_email_verification`,
  抛 `EmailNotVerified`;`app/main.py:186`–`:199` 把它渲染成
  **页面 303 → `/verify-email`** / **`/api` 403 `email_not_verified`**
- **收件人**:`app/accounts.py::sync_user_recipient` 只认「有邮箱**且**已验证」,
  验证通过、改邮箱、清空邮箱三条路径都靠它同步
- **固定地址**:账号创建与引导回填登记 `feed_tokens` 一行,
  见 `docs/static/app-feed-tokens.md` 与 `docs/static/app-bootstrap.md`
- **数据列**:`users.email_verified_at` 等 5 列由迁移
  `0007_email_verification` 加出,见 `docs/static/migrations-0007.md`

## 没有测试覆盖的点

- `generate_code()` 的「允许前导零」与 `CODE_LENGTH` 位数没有直接断言,
  只有验证码邮件里 `\b(\d{6})\b` 的正则间接覆盖(带前导零的码不会被特意构造)。
- `code_hash()` 的载荷格式 `verify:<user_id>:<code>` 没有「换格式即失效」的断言,
  也没有「同一验证码对不同账号得到不同哈希」的用例。
- `"too_many"` 分支没有用例(默认配置下第 5 次错误即作废,下一次是 `missing`)。
- `needs_email_verification` 的**第 2 条豁免**(没有邮箱的账号不受阻)没有专门用例。
- `send_code_mail` / `send_subscription_mail` 在 **SMTP 未配置**时返回 `False`
  的分支没有直接断言(注册流程在测试里都配了 SMTP)。

## 变更记录

- 2026-10-12:新增本文档。归档邮箱验证的完整状态(无需验证/待验证/已验证)、
  `check_code` 的五个返回值与「第 5 次错误即作废」的实现路径、
  `needs_email_verification` 的三条判定(管理员豁免、无邮箱不挡、有邮箱未验证才挡)、
  两封信(`send_code_mail` / `send_subscription_mail`)与
  「验证码与固定地址都不进日志/审计」的约束。