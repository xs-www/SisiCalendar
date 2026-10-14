# `app/urls.py`

公开面的**路径前缀**与**链接拼装**。家庭成员用的所有地址都挂在 `PUBLIC_BASE_URL` 的
路径部分下(默认 `/calendar`):`/calendar/register`、`/calendar/login`、
`/calendar/verify-email`、`/calendar/bootstrap`、`/calendar/admin/...`、
`/calendar/api/...`、`/calendar/static/...` 以及每人的订阅地址
`/calendar/<令牌>.ics`。本机管理面(`http://localhost:443/admin`)仍走**裸路径**,
两套路由同时注册(带前缀的那套 `include_in_schema=False`,见 `app/main.py:145`–`:155`)。

前缀**不写死成常量**,而是按每次请求的路径推导:同一个应用既服务 `/admin`(本机)
又服务 `/calendar/admin`(家庭成员),页面里的链接必须跟着入口走。推导结果存在
`ContextVar` 里,由 `app/main.py` 的 HTTP 中间件在进入路由**之前**绑定
(`app/main.py:161`)。模板侧用全局函数 `bp()` / `url()`(`app/deps.py:65`–`:66`)。

绝对地址(邀请链接、每人固定订阅地址、邮件正文里的验证页与订阅地址)一律从
`PUBLIC_BASE_URL` 派生:见 `invite_url()` 与 `subscription_url()`。它取代了原先的
`SUBSCRIPTION_URL` 配置项(已删除)。

## 常量与上下文

### `_PREFIX: ContextVar[str]`

- **位置**:`app/urls.py:25`
- **取值与含义**:当前请求生效的前缀,默认 `""`(裸路径);带前缀入口下是 `/calendar`
- **不变量**:每个请求都必须在**路由之前**重新绑定(`app/main.py:161`,中间件),
  否则模板会渲染出裸链接,家人点一下就被甩回只在本机可达的路径;
  它是**请求级**状态,不得当成进程级常量缓存

### 常量 `FEED_SUFFIX` / `INVITE_QUERY`

- **位置**:`app/urls.py:28` / `app/urls.py:103`
- **取值与含义**:`FEED_SUFFIX = ".ics"`(订阅地址的固定后缀,路由也只匹配这个形状);
  `INVITE_QUERY = "code"`(邀请链接的参数名,是
  `app/invites.py::INVITE_PARAM` 的镜像,为避免循环导入而各留一份)
- **不变量**:两处参数名必须保持一致,否则邀请链接会被注册页当成「没有带码」;
  `FEED_SUFFIX` 不是秘密,不得作为鉴权条件(鉴权只认 `feed_tokens.token_hash`)

## 函数

### `normalize_prefix(value) -> str`

- **位置**:`app/urls.py:31`
- **用途**:把任意前缀文本规范成 `""` 或 `/xxx`(无尾斜杠)
- **参数**:`value` 接受任意对象(`None`、非字符串都先 `str()`,再 `strip()`)
- **返回**:`""` / `"/"` / 纯空白 → `""`;其余补上开头的 `/` 并去掉末尾的 `/`
- **副作用**:无
- **不变量**:返回值只有两种形态(空串或以 `/` 开头且不以 `/` 结尾),
  调用方不必再自己拼斜杠
- **测试**:`tests/test_public_prefix.py::test_base_url_without_path_falls_back_to_bare_paths`
  (基地址没有路径 → `""` → 整套路由退回裸路径)

### `public_base_url(settings) -> str`

- **位置**:`app/urls.py:41`
- **用途**:对外公开的基地址(无尾斜杠),用于生成可直接复制粘贴的绝对链接
- **参数**:读 `settings.public_base_url`(`PUBLIC_BASE_URL`,默认
  `https://sisi-laptop.tail921448.ts.net/calendar`)
- **返回**:去空白、去尾斜杠的基地址;配置为空时返回 `""`
- **副作用**:无
- **不变量**:基地址是**拼绝对地址的唯一来源**;除它之外不得再引入第二套订阅挂载点配置
- **测试**:`tests/test_public_prefix.py::test_invite_link_contains_public_base_url`、
  `::test_subscription_url_comes_from_public_base_url`
- **调用方**:`app/main.py:156`(启动日志)、`app/email_verify.py:180`(验证码邮件里的验证页地址)、
  `app/sync_service.py:220`、`app/routers/api_settings.py:39`、`app/routers/pages.py:162`、
  `:595`、`:779`、`:780`、`:1585`

### `public_base_path(settings) -> str`

- **位置**:`app/urls.py:46`
- **用途**:基地址里的**路径部分**,即公开面的挂载前缀(如 `/calendar`)
- **返回**:`normalize_prefix(urlsplit(public_base_url(settings)).path)`;域名只有主机名时返回 `""`
- **副作用**:无
- **不变量**:**挂载路径必须与后端路径逐字一致**(本仓库不做前缀剥离),
  因此 `PUBLIC_BASE_URL` 的路径部分就是 `app/main.py` 挂载时用的那一段
- **测试**:`tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`、
  `::test_base_url_without_path_falls_back_to_bare_paths`
- **调用方**:`app/main.py:126`(路由挂载与中间件绑定)、
  `app/config.py:145` 的同名属性 `Settings.public_base_path`

### `bind_prefix(path, base_path) -> str`

- **位置**:`app/urls.py:51`
- **用途**:按本次请求路径判断前缀并绑定到当前上下文
- **参数**:`path` 是请求的原始路径(`request.url.path`);`base_path` 是
  `public_base_path(settings)`
- **返回**:本次绑定的前缀(命中 `base_path` 本身或 `base_path + "/"` → `base_path`,否则 `""`)
- **副作用**:写 `_PREFIX`(当前上下文的 `ContextVar`)
- **不变量**:判定只看路径,不看头、不看来源地址——Docker NAT 之下本机请求与代理请求
  同源(`AGENTS.md` 的部署陷阱 3),靠客户端地址区分入口是不可行的;
  `base_path` 为空串时**永远**绑定 `""`(整套路由退回裸路径)
- **测试**:`tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`、
  `::test_local_bare_surface_still_works`、`::test_prefixed_login_redirects_back_into_the_prefix`
- **调用方**:`app/main.py:161`(HTTP 中间件,`security_headers`)

### `current_prefix() -> str`

- **位置**:`app/urls.py:60`
- **用途**:取当前请求生效的前缀
- **返回**:`_PREFIX.get()`;未绑定过的上下文返回 `""`
- **副作用**:无
- **不变量**:只读;**不要**在请求之外把它当常量用
- **测试**:由 `tests/test_public_prefix.py::test_prefixed_login_redirects_back_into_the_prefix` 间接覆盖
- **调用方**:`app/deps.py:65`(注册成模板全局 `bp`)、`app/routers/pages.py:222`(`root()`)

### `url(path) -> str`

- **位置**:`app/urls.py:65`
- **用途**:站内链接(重定向地址、模板里的 `action`/`href`):加上当前请求的前缀
- **参数**:`path` 不以 `/` 开头时自动补;不处理查询串(调用方自己拼 `?next=...`)
- **返回**:`f"{当前前缀}{path}"`;裸路径入口下就是 `path` 本身
- **副作用**:无
- **不变量**:所有站内跳转都必须经过它(或模板全局 `url()`),**不得**手写 `/admin`、
  `/login` 这类裸字面量,否则带前缀入口下的用户会被甩到本机路径
- **测试**:`tests/test_public_prefix.py::test_prefixed_login_redirects_back_into_the_prefix`
  (未登录访问 `/calendar/admin` → `303` 到 `/calendar/login?next=/calendar/admin`)、
  `tests/test_email_verify.py::test_unverified_member_is_blocked`
  (闸门把页面送到带前缀的 `/verify-email`)
- **调用方**:`app/deps.py:66`(模板全局 `url`)、`app/main.py:182`、`:183`、`:199`、
  `app/routers/auth.py:69`、`:199`、`:204`、`:213`、`:253`、`:421`、`:468`、`:511`、`:524`、`:557`、
  `app/routers/pages.py:210`、`:225`

### `relative(path) -> str`

- **位置**:`app/urls.py:72`
- **用途**:去掉当前请求前缀,得到应用内部的规范路径(供前缀无关的判断)
- **返回**:命中前缀(等于前缀或以 `前缀 + "/"` 开头)时返回剥掉后的部分,
  剥成空串则返回 `"/"`;未命中时原样返回
- **副作用**:无
- **不变量**:前缀相关的**判断**必须走它(`is_api_path`/`is_admin_path` 已经这样实现),
  否则带前缀的请求会被误判
- **测试**:由 `tests/test_public_prefix.py::test_prefixed_json_api_reports_401_in_json`
  与 `::test_admin_pages_under_prefix_link_within_the_prefix` 间接覆盖
- **调用方**:仅 `app/urls.py` 内部(`is_api_path`、`is_admin_path`)

### `is_api_path(path) -> bool`

- **位置**:`app/urls.py:80`
- **用途**:判断是不是 JSON 接口:裸 `/api/...` 与 `/calendar/api/...` 都算
- **返回**:`relative(path).startswith("/api/")`
- **副作用**:无
- **不变量**:它决定**错误响应的形状**(异常处理器据此返回 JSON 而不是 303/HTML),
  因此 `/api/agent` 也必须算进来(它是 Bearer-only 的 JSON 面,不含 Cookie 会话);
  判定必须基于 `relative()`,不能直接看原始路径
- **测试**:`tests/test_public_prefix.py::test_prefixed_json_api_reports_401_in_json`、
  `tests/test_email_verify.py::test_unverified_member_is_blocked`
  (`/api/v1/events` 返回 403 `email_not_verified` 而不是重定向)、
  `::test_event_creation_by_unverified_account_is_rejected`
- **调用方**:`app/deps.py:122`、`app/main.py:177`、`:191`、`:203`、`:215`

### `is_admin_path(path) -> bool`

- **位置**:`app/urls.py:85`
- **用途**:判断是不是需要挂**严格 CSP** 的管理/登录页
- **返回**:`relative(path).startswith(("/admin", "/login", "/register", "/verify-email", "/bootstrap"))`
  (`app/urls.py:88`–`:90`;集合里最后一项 `/bootstrap` 是 2026-10-14 新增的)
- **副作用**:无
- **不变量**:判定是普通**前缀匹配**;集合里必须包含 `/verify-email`(验证页同样是
  无内联脚本的表单页)与 **`/bootstrap`**(首位管理员的引导码设置页同样自带完整 HTML
  shell、无内联脚本,见 `templates/bootstrap.html`),漏掉任何一个都会让它失去 CSP;
  它们的判定都基于 `relative()`,所以 `/calendar/bootstrap` 这类带前缀入口也被算进来
- **测试**:`tests/test_family_web.py::test_register_page_gets_csp`、
  `tests/test_public_prefix.py::test_admin_pages_under_prefix_link_within_the_prefix`、
  `tests/test_bootstrap_access.py::test_bootstrap_page_serves_the_form_with_csp`
  (引导页 200 且响应头含 `Content-Security-Policy`)
- **调用方**:`app/main.py:165`(中间件据此设置 `Content-Security-Policy`)

### `invite_url(settings, code) -> str`

- **位置**:`app/urls.py:90`
- **用途**:邀请链接:`{基地址}/register?code={邀请码明文}`,管理员可直接复制粘贴给家人
- **参数**:`code` 走 `urllib.parse.quote`;`INVITE_QUERY` 决定参数名
- **返回**:完整绝对地址;`public_base_url` 为空时退化成 `/register?code=...`
- **副作用**:无
- **不变量**:这是邀请码明文出现的**允许位置之一**(创建响应与这条链接),但
  **不得**写进日志或审计 detail;链接前缀取自 `PUBLIC_BASE_URL`,不跟当前请求走
  (复制出去的链接必须对家人可用)
- **测试**:`tests/test_public_prefix.py::test_invite_link_contains_public_base_url`
  (创建响应与列表里都是 `https://example.test/calendar/register?code=sisi_invite_...`)、
  `tests/test_family_pages.py::test_invite_plaintext_is_shown_once_and_register_link_works`
- **调用方**:`app/routers/pages.py:895`

### `asset(path) -> str` / `asset_version(relative_path) -> str`(2026-10-12 新增)

- **作用**:把静态资源渲染成**带内容指纹**的地址,如
  `/static/css/app.css?v=3a1f-68f2b7c0`(自动带当前请求前缀)。
- **实现**:`asset_version()` 取 `static/<path>` 的 `大小-mtime` 十六进制(取不到返回 `0`),
  结果缓存在模块级 `_ASSET_STAMPS`(进程内一次,静态文件只在部署时变);
  `asset()` 用 `url('/static/<path>')` 拼前缀再追加 `?v=`。
- **为什么必须有**:浏览器对静态资源有长期缓存。地址不带指纹时,改了 CSS/模板而地址不变,
  用户会继续用旧样式——真机上表现为「页面错版」,而且刷新也不一定好(需要硬刷新)。
- **调用方**:`app/deps.py` 注册的 Jinja 全局 `asset`;模板 `templates/base.html`、
  `calendar.html`、`event_form.html`、`login.html`、`register.html`、`verify_email.html`。
- **不变量**:模板里**不得**再写 `{{ bp() }}/static/...` 这种不带指纹的资源地址。
- **测试**:`tests/test_family_pages.py::test_static_assets_carry_a_version_stamp`
  (正则要求 `?v=<hex>-<hex>`,并实际请求一次确认 200)。

## `subscription_url(settings, token) -> str`

- **位置**:`app/urls.py:96`
- **用途**:订阅地址:每个账号一条固定地址,`<基地址>/<令牌>.ics`
- **参数**:`token` 是订阅令牌**明文**(账号固定地址由
  `app/feed_tokens.py::account_feed_token` 派生,临时地址由 `create_feed_token` 生成)
- **返回**:完整绝对地址,形如 `https://<节点>/calendar/sisi_feed_xxx.ics`;
  基地址为空时退化成 `/<令牌>.ics`
- **副作用**:无
- **不变量**:地址形态固定为 `<基地址>/<令牌>.ics`,与 Tailscale Serve 上唯一的
  `/calendar` 挂载天然一致(不需要前缀剥离);**不带任何查询参数**,避免 iPhone
  把地址当成 HTML 页面;明文不得写日志与审计(`AGENTS.md` 不变量 18、23)
- **测试**:`tests/test_public_prefix.py::test_subscription_url_comes_from_public_base_url`
  (拼出来的地址在本机也真的能取到内容)、
  `tests/test_family_accounts.py::test_subscription_url_uses_configured_base`、
  `tests/test_family_pages.py::test_my_feed_is_stable_and_temporary_token_shown_once`、
  `tests/test_family_mail.py::test_owner_recipient_gets_his_own_fixed_address`、
  `::test_member_recipient_gets_only_his_own_address`
- **调用方**:`app/feed_tokens.py:106`、`:218`、`app/mailqueue.py:272`、
  `app/routers/pages.py:589`

## 与路由装配的配合

> 路由与中间件的完整契约见 `docs/static/app-main.md`;此处只列与本模块相关的部分。

- **位置**:`app/main.py:126`(`base_path`)、`:128`(`/static` 裸挂载)、
  `:145`–`:155`(带前缀挂载)
- **两套入口**:`/static` 固定挂载;`base_path/static` 在基地址带路径时才挂载。
  所有 router 先按裸路径注册一份,再在 `base_path` 非空时用 `prefix=base_path`
  注册第二份(`include_in_schema=False`,公开文档只描述裸路径那一套);
  `base_path` 本身额外注册 `pages.public_root`(GET/HEAD),避免 Starlette 先来一次 307
- **前缀本身 `/calendar`**(无令牌旧地址)的行为见
  `app/routers/pages.py:194` 的 `FEED_PLACEHOLDER_TEXT` 与 `:200` 的 `public_root`:
  日历客户端(`Accept` 不含 `text/html`)拿到 **404 纯文本**,
  浏览器(明确要 `text/html`)拿到 **303 到 `/calendar/admin`**
- **测试**:`tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`、
  `::test_base_url_without_path_falls_back_to_bare_paths`

## 没有测试覆盖的点

- `relative()` 被直接断言的用例没有:它只经 `is_api_path` / `is_admin_path` 间接覆盖。
- `normalize_prefix()` 对「非字符串输入」(`None`、数字)的归一化没有专门用例。
- `is_admin_path()` 里 `/verify-email` 这一项没有直接断言 CSP 头的用例,
  只有「验证页可以打开、页面表单可用」的间接覆盖
  (`/bootstrap` 这一项在 2026-10-14 有了直接断言:
  `tests/test_bootstrap_access.py::test_bootstrap_page_serves_the_form_with_csp`)。
- `url()` 对**不含前导斜杠**路径自动补齐的行为没有专门用例(调用方都传了 `/...`)。

## 变更记录

- 2026-10-12(第二轮):新增 `asset()` / `asset_version()`,给静态资源加内容指纹,
  治「改了样式但浏览器还在用旧 CSS」的错版问题;`__all__` 相应加入两个名字。
- 2026-10-12:新增本文档。归档公开面统一到 `PUBLIC_BASE_URL` 路径前缀
  (`app/urls.py` 取代旧的 `SUBSCRIPTION_URL` 常量):请求级前缀绑定
  (`_PREFIX` / `bind_prefix` / `current_prefix`)、站内链接拼装(`url` / `relative`)、
  路径分类(`is_api_path` / `is_admin_path`,决定错误响应形状与 CSP)、
  绝对地址派生(`invite_url` / `subscription_url`),以及 `/calendar` 前缀本身
  对日历客户端 404、对浏览器 303 的边界。
- 2026-10-14:`is_admin_path` 的路径集合增加 **`/bootstrap`**
  (`app/urls.py:88`–`:90`),首位管理员的引导码设置页因此也拿到严格 CSP;
  `templates/bootstrap.html` 与 `app/routers/auth.py` 的两条 `/bootstrap` 路由都
  按当前请求前缀渲染(`bp()` / `urls.url()`),带前缀入口 `/calendar/bootstrap` 同样可用。
  见 [`app-bootstrap-access.md`](app-bootstrap-access.md)、
  [`app-routers-auth.md`](app-routers-auth.md)。