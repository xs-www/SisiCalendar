# `app/main.py`

应用装配层:`create_app`、lifespan(引导 + 后台调度)、安全响应头、异常页与路由挂载。
本文件归档本轮(2026-10-12)改动 —— **公开面前缀绑定**、**双份路由注册**、
**`/calendar` 前缀根由 `pages.public_root` 处理**、**`EmailNotVerified` 处理器**,
以及既有的 403 处理器与 CSP 路径集合。

## 函数 / 常量

### `SECURITY_HEADERS`

- **位置**:`app/main.py:36`
- **内容**:`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、
  `X-Frame-Options: DENY`
- **本次变更**:无
- **测试**:`tests/test_page_security.py::test_common_security_headers`

### `ADMIN_CSP`

- **位置**:`app/main.py:42`
- **内容**:`default-src 'self'`、`img-src 'self' data:`、`font-src 'self' data:`、
  `style-src 'self' 'unsafe-inline'`、`script-src 'self'`、`connect-src 'self'`、
  `form-action 'self'`、`frame-ancestors 'none'`、`base-uri 'self'`
- **本次变更**:无(CSP 字符串本身没动,变的是**哪些路径**会带上它)
- **不变量**:**`script-src 'self'` 之下没有任何内联脚本的余地** ——
  模板里不得出现 `<script>…</script>` 正文或 `onclick=`/`onsubmit=` 之类的事件属性,
  所有前端逻辑放 `static/js/`,前端依赖必须同源自托管,需要传给 JS 的值走 `data-*` 属性
- **为什么显式写 `font-src 'self' data:`**:FullCalendar 的上/下月箭头是内嵌 base64 字体,
  不声明就会回退到 `default-src 'self'` 被拦掉,按钮退化成方块
- **测试**:`tests/test_page_security.py::test_csp_blocks_external_scripts_and_plugins`、
  `::test_csp_allows_the_embedded_icon_font`、`::test_vendored_frontend_assets_are_self_hosted`

### `create_app(settings=None) -> FastAPI`

- **位置**:`app/main.py:106`;公开前缀取自 `urls.public_base_path(resolved)`(`app/main.py:126`)
- **本次变更(2026-10-12)**:
  - **同一套路由注册两遍**:先按裸路径 `app.include_router(router)`(`app/main.py:144`),
    再在 `base_path` 非空时 `app.include_router(router, prefix=base_path, include_in_schema=False)`
    (`app/main.py:155`)。因此 `/login` 与 `/calendar/login`、`/api/v1/...` 与
    `/calendar/api/v1/...` 都是同一条处理器;裸路径留给本机
    `http://localhost:443/admin`,带前缀的给成员
  - **静态资源两处挂载**:`/static`(`app/main.py:128`)与 `f"{base_path}/static"`
    (name=`static-public`,`app/main.py:146`),后者的目录是同一份 `STATIC_DIR`
  - **前缀根本身单独加一条路由**:`app.add_api_route(base_path, pages.public_root,
    methods=["GET", "HEAD"], include_in_schema=False)`(`app/main.py:148`),
    避免 Starlette 先把 `/calendar` **307** 到 `/calendar/`(那会让日历客户端把重定向
    当成订阅结果)。带尾斜杠的 `/calendar/` 走 `pages.router` 的 `GET /`(`pages.root`)
  - **日志**:`logger.info("公开面挂载前缀:%s(基地址 %s)", base_path, urls.public_base_url(resolved))`
    (`app/main.py:156`);`base_path` 为空(基地址没有路径部分)时**整段跳过**,
    连 `/calendar/...` 都不存在
- **中间件(`app/main.py:158`)**:
  - **先绑定前缀再进路由**:第一句就是 `urls.bind_prefix(request.url.path, base_path)`
    (`app/main.py:161`)——模板里的 `bp()`/`url()` 与重定向都靠这个 `ContextVar`,
    必须在路由之前定下来
  - 然后 `setdefault` 铺一遍 `SECURITY_HEADERS`(所有响应都有)
  - 最后 `if urls.is_admin_path(request.url.path)` → `setdefault("Content-Security-Policy", ADMIN_CSP)`。
    `is_admin_path` 内部先 `relative()` 去掉当次请求前缀,所以 **`/admin`、`/login`、
    `/register`、`/verify-email` 及其 `/calendar/...` 形式都会带上 CSP**
- **不变量**:
  - **订阅命名空间不得被管理页污染**:`/calendar`(前缀本身)对日历客户端是
    **404 纯文本 + `Cache-Control: no-store`**,对浏览器(`Accept` 含 `text/html`)是
    **303 → `/calendar/admin`**;`/calendar.ics` 与旧的无令牌 `/calendar` **不再提供日历**
    (见 `AGENTS.md` 不变量 8 与 14)
  - 订阅入口 `/calendar/<令牌>.ics` **不得**带上管理页的 CSP(它是给日历客户端消费的 ICS)
  - 用 `setdefault` 而不是赋值:路由自己设过 CSP 时不要覆盖
  - 挂载路径必须与 `PUBLIC_BASE_URL` 的路径部分逐字一致(本仓库不做前缀剥离)
- **测试**:`tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`、
  `::test_local_bare_surface_still_works`、
  `::test_base_url_without_path_falls_back_to_bare_paths`(基地址无路径时
  `/calendar/login` 必须 404,不留半个公开面)、
  `::test_prefix_root_serves_text_404_to_calendar_clients`、
  `::test_admin_pages_under_prefix_link_within_the_prefix`、
  `tests/test_family_web.py::test_legacy_tokenless_address_is_gone`、
  `tests/test_feed.py::test_legacy_tokenless_paths_are_gone`、
  `tests/test_page_security.py::test_csp_present_on_admin_pages`(`CSP_PAGES` 逐个断言
  `/admin`、`/admin/settings`、`/admin/sync`、`/admin/events/new`)、
  `::test_login_page_also_gets_csp`、`::test_feed_is_not_served_with_admin_csp`、
  `tests/test_family_web.py::test_register_page_gets_csp`

### `_install_exception_pages(app)` / 四个异常处理器

- **位置**:`_install_exception_pages` 在 `app/main.py:172`;
  `NotAuthenticated`(`:173`)、`EmailNotVerified`(`:186`)、`CsrfFailure`(`:201`)、
  `Forbidden`(`:213`)
- **本次变更(2026-10-12)**:
  - **新增 `EmailNotVerified` 处理器**(`app/main.py:186`):邮箱还没验证的已登录账号 ——
    - `urls.is_api_path(request.url.path)` → **403** `error_payload("email_not_verified", …)`
      (账号已登录,只是还不能用,所以不是 401);
    - 其余路径 → **303 → `urls.url("/verify-email")`**,把它送到验证页而不是给一堵墙
  - **`NotAuthenticated` 处理器改为前缀感知**(`app/main.py:173`):
    未登录页面请求 303 到 **同一个前缀**下的 `urls.url("/login")`,
    带上 `?next=`(取 `exc.next_url`,缺省 `urls.url("/admin")`,`quote(..., safe="/")`);
    `/api` 请求仍是 401 `unauthorized`
  - `CsrfFailure`(`:201`)与 `Forbidden`(`:213`)的 API/页面分流也改用 `urls.is_api_path`,
    因此 `/calendar/api/...` 同样返回 JSON 错误体
- **`Forbidden` 行为**:路径是 `/api/` → `JSONResponse(error_payload("forbidden", exc.message), 403)`,
  与 `NotAuthenticated`(401)、`CsrfFailure`(403)同构,即所有 JSON 接口的错误体都是统一的
  `{"error": {...}}`;其余路径 → `HTMLResponse("<h1>403</h1><p>{exc.message}。请返回日历页,
  或换一个有权限的账号。</p>", 403)`,不做重定向
- **副作用**:无(只构造响应)
- **不变量**:
  - **403 与 404 的分工不得混用**:成员访问 `/admin/members`、`/admin/settings`、
    `/admin/sync` 及其片段是 **403**(接口存在但没权限);越权读取**事件**
    (`/admin/events/{id}`、`GET /api/v1/events/{id}`)是 **404**(连存在性都不暴露)。
    见 `AGENTS.md` 不变量 8 与 13
  - 处理器不吞异常类型:每个处理器只挂在自己的异常上
  - **验证闸门不能把验证页自己挡掉**:`/verify-email` 的四个路由走
    `AnyUserDep` / `CsrfAnyDep`(见 `docs/static/app-deps.md`)
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`
  (页面 303 → `/verify-email`、`/api/v1/events` 403 `email_not_verified`、验证页本身 200)、
  `::test_event_creation_by_unverified_account_is_rejected`(写接口同样被闸门拦下)、
  `tests/test_public_prefix.py::test_prefixed_login_redirects_back_into_the_prefix`、
  `::test_prefixed_json_api_reports_401_in_json`、
  `tests/test_family_web.py::test_admin_only_pages_reject_members`
  (参数化 `/admin/settings`、`/admin/sync`、`/admin/members`,断言 `in (403, 404)`)、
  `::test_forbidden_returns_users_language_for_pages_and_json_for_api`
  (页面正文含 `403` 与「权限」,`/api/v1/settings` 的 `error.code == "forbidden"`)、
  `tests/test_family_pages.py::test_member_is_locked_out_of_admin_surfaces`、
  `::test_member_cannot_use_admin_apis`

### `_log_credentials(settings)`

- **位置**:`app/main.py:66`
- **本次变更**:无 —— 文案仍为「首次启动已生成**家庭**管理员口令(只显示这一次)」,
  并在提示末尾加一行「登录后请在『我的设置』里修改口令,并用邀请码添加家庭成员。」
- **不变量**:口令明文**只允许**出现在这一段首次启动日志里;用户名/口令不写库、不回显页面
- **测试**:`tests/test_page_security.py` 不覆盖日志;相关约束由
  `tests/test_mail.py::test_smtp_password_is_never_persisted` 之类的"敏感值不入库"用例
  从侧面保证 —— **本函数没有直接用例**

## 影响面

- 前缀是**按请求**绑定的,所以任何在路由之前读 `urls.current_prefix()` 的代码都会拿到空串;
  页面上下文与重定向一律通过 `bp()` / `urls.url()` 取,不要自己拼 `/calendar`
  (见 `docs/static/app-urls.md`)。
- 异常处理器是权限与验证模型的收口:页面层(`AdminDep` / `CsrfAdminDep` / `UserDep` / `CsrfDep`)
  抛 `Forbidden` / `NotAuthenticated` / `EmailNotVerified`,由这里决定给 HTML 还是 JSON;
  **路由层不要自己 `return HTMLResponse(403)`**,否则两条路径(页面 / 接口)的错误体就会不一致。
- CSP 路径集合与 `templates/` 的"无内联脚本"约定是一对:
  一旦有人在 `/register`、`/verify-email` 里塞内联脚本,浏览器会静默拦掉而页面不报错,
  所以模板侧有 `tests/test_family_pages.py` 的正则断言兜底
  (`INLINE_HANDLER` / `INLINE_SCRIPT`,见 `docs/static/templates.md`)。

## 测试缺口

- `/verify-email` 的 CSP:`is_admin_path` 已把它纳入,但**没有**用例把
  `/verify-email`(或 `/calendar/verify-email`)加进 `tests/test_page_security.py::CSP_PAGES`
  或单独断言 —— 现有 CSP 用例只覆盖 `/admin/*`、`/login` 与 `/register`
  (`tests/test_family_web.py::test_register_page_gets_csp`)。
- `/calendar` 前缀根路由(`app.add_api_route(base_path, pages.public_root, ...)`)的
  **浏览器 303 分支**由 `tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`
  覆盖,但覆盖的是带尾斜杠的 `/calendar/`(走 `pages.root`);**不带尾斜杠**的
  `/calendar` + `Accept: text/html` 没有单独断言。
- `_log_credentials` 的家庭化文案:无用例(日志文案本来也没有测试在守)。

## 变更记录

- 2026-10-08:新建本文档。归档 `create_app` 中间件的 CSP 路径集合扩展
  (加入 `/register`)、`_install_exception_pages` 新增 `Forbidden` 403 处理器
  (API → JSON `{"error": {"code": "forbidden"}}`,页面 → 人话 HTML),
  以及首次启动日志文案的家庭化;同时写明 `script-src 'self'` 下不得有内联脚本这一硬约束。
- 2026-10-12:公开面统一到 `/calendar` 前缀。记录:路由注册两遍(裸的一套 +
  `prefix=base_path, include_in_schema=False` 的一套)、`f"{base_path}/static"` 静态挂载、
  `add_api_route(base_path, pages.public_root)` 防 307、中间件在路由前
  `urls.bind_prefix(...)`、CSP 判定改用前缀感知的 `urls.is_admin_path`、
  `NotAuthenticated`/`CsrfFailure`/`Forbidden` 的分流改用 `urls.is_api_path`;
  新增 `EmailNotVerified` 处理器(页面 303 → `/verify-email`,API 403 `email_not_verified`)。
  同时按当前源码校正全部行号,并把 `/register` CSP 与 403 响应体的测试缺口标记为已覆盖。