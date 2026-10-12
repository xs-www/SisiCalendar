# `app/deps.py`

FastAPI 依赖与 Jinja2 模板环境。涉及模板全局函数 `bp()`/`url()`、模板过滤器 `localtime`、
当前登录账号与 CSRF/管理员依赖、**邮箱验证闸门**,以及 Agent 令牌鉴权。

家庭版(2026-10)起有两级主体:**成员**(任何登录账号)与**管理员**;
权限规则本身不在这里,一律走 `app/accounts.py`。

## 变更(2026-10-12:公开前缀与邮箱验证闸门)

### `templates.env.globals["bp"]` / `templates.env.globals["url"]`

- **位置**:`app/deps.py:65` / `app/deps.py:66`(`templates` 环境在 `app/deps.py:43`)
- **用途**:模板里的链接前缀与站内链接
  - `bp`:`app/urls.py::current_prefix` —— 模板写 `{{ bp() }}/admin/me`;
    从裸路径进来是 `""`,从 `/calendar/...` 进来是 `"/calendar"`
  - `url`:同一个函数,给需要"已经拼好前缀的完整站内路径"的模板用
- **为什么是**全局函数而不是常量**:同一个应用同时服务两种入口
  (见 `docs/static/app-urls.md` 与 `docs/static/app-main.md` 的中间件),
  前缀由 `urls.bind_prefix` 按**当次请求**写进 `ContextVar`
- **不变量**:模板里**不得**硬编码 `/calendar/...`;所有站内链接都经 `bp()` / `url()`,
  否则本机管理页会把家人甩到不可达的路径上

### `templates.env.globals["asset"]`(2026-10-12 新增)

- **位置**:`app/deps.py:67`(`= app/urls.py::asset`)
- **用途**:静态资源地址 + 内容指纹,模板写 `{{ asset('css/app.css') }}`
  → 渲染成 `/static/css/app.css?v=<大小>-<mtime>`(自动带当次请求前缀)
- **动机**:浏览器长期缓存 CSS/JS,地址不变时改完样式用户还是看到旧版(真机反馈的
  「侧栏错版」);带指纹后部署即失效旧缓存,不需要让家人手动硬刷新
- **不变量**:模板里**不得**再写 `{{ bp() }}/static/...` 这类不带指纹的资源地址
- **测试**:`tests/test_family_pages.py::test_static_assets_carry_a_version_stamp`
- 详见 `docs/static/app-urls.md` 的 `asset()` 一节
- **测试**:`tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`、
  `::test_local_bare_surface_still_works`、
  `::test_admin_pages_under_prefix_link_within_the_prefix`

### `EmailNotVerified`

- **位置**:`app/deps.py:89`
- **用途**:已登录但邮箱还没验证(见 `AGENTS.md` 不变量 22)。由
  `app/main.py:186` 的处理器转成响应:**`/api/*` → 403 `email_not_verified`**;
  **其他路径 → 303 → `/verify-email`**
- **参数**:无(不带任何载荷,避免把邮箱或验证码带进异常)
- **不变量**:验证页自身与「重新发送验证码」「改邮箱」必须**不受**这道闸门影响
  (它们用 `require_any_user` / `require_csrf_any`),否则会自己把自己挡在门外;
  异常里不得出现验证码或邮箱明文
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`、
  `::test_event_creation_by_unverified_account_is_rejected`

### `_gate_verification(current) -> CurrentUser`

- **位置**:`app/deps.py:145`
- **用途**:所有"登录即可用"的依赖共用的闸门:`app/email_verify.py::needs_email_verification`
  为真时抛 `EmailNotVerified`,否则原样返回
- **判定规则**(在 `app/email_verify.py` 里):**有邮箱且 `email_verified_at` 为空**的
  **普通成员**被挡住;**管理员豁免**(本机救急不能被自己的邮箱锁在门外);
  **没有邮箱的账号不挡**(升级上来的老账号可能没填,由界面持续提醒)
- **返回**:传入的 `CurrentUser`(便于写成 `return _gate_verification(current)`)
- **副作用**:无
- **不变量**:闸门只在"取当前用户"这一层存在;它**不**改写权限判定,
  也不得让 `/verify-email` 的四个路由经过它
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`、
  `::test_unverified_admin_is_not_blocked`

### `require_any_user(request, session=Depends(get_db)) -> CurrentUser`

- **位置**:`app/deps.py:158`
- **用途**:已登录即可,**不**检查邮箱验证:验证页与改邮箱 / 重发验证码专用
- **返回**:`CurrentUser`;未登录仍抛 `NotAuthenticated`(`next_url` 是当次请求路径)
- **副作用**:一次会话解析 + 一次账号主键查询
- **不变量**:**除了** `/verify-email` 相关的四个路由与 `POST /logout`,
  任何业务页面/接口都必须用 `require_user`(带闸门),不要为了"少一次跳转"改用本函数
- **测试**:`tests/test_email_verify.py::test_unverified_member_is_blocked`
  (未验证时 `/verify-email` 仍 200)、`::test_unverified_admin_is_not_blocked`

### `require_csrf_any(request, session=Depends(get_db)) -> CurrentUser`

- **位置**:`app/deps.py:203`
- **用途**:校验会话与 CSRF,**不**检查邮箱验证(验证码提交、重发与改邮箱用)
- **错误语义**:无会话 → `NotAuthenticated`;令牌不符 → `CsrfFailure`;
  账号不存在/停用 → `NotAuthenticated`(**邮箱未验证不是错误**)
- **与 `require_csrf` 的区别**:`require_csrf` 在最后一步调 `_gate_verification`,
  本函数直接返回 `current`
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`、
  `::test_resend_cooldown_then_new_code`、`::test_can_fix_a_mistyped_email_from_the_verify_page`

### `AnyUserDep` / `CsrfAnyDep`

- **位置**:`app/deps.py:244`(`AnyUserDep = Annotated[CurrentUser, Depends(require_any_user)]`)、
  `app/deps.py:247`(`CsrfAnyDep = Annotated[CurrentUser, Depends(require_csrf_any)]`)
- **用途**:`GET /verify-email` 用 `AnyUserDep`;`POST /verify-email`、
  `POST /verify-email/resend`、`POST /verify-email/email` 用 `CsrfAnyDep`
  (见 `docs/static/app-routers-auth.md`)
- **不变量**:这两个别名是**闸门豁免**的全部入口;新增豁免必须有明确理由

### `is_api_request(request) -> bool`

- **位置**:`app/deps.py:121`
- **本次变更**:实现改为**前缀感知**的一句话 —— `urls.is_api_path(request.url.path)`,
  内部先 `relative()` 去掉当次请求前缀,所以 `/api/v1/...` 与 `/calendar/api/v1/...` 都算接口
- **返回**:`True` 表示这是 JSON 接口(未登录 401、CSRF 失败与权限不足给 JSON 错误体)
- **不变量**:接口与页面的分流判据只有这一处,不要在路由里再写 `path.startswith("/api")`

## 变更(2026-10-08:家庭版当前用户、CSRF 与管理员依赖)

### `CurrentUser(user, csrf_token)`

- **位置**:`app/deps.py:98`(frozen dataclass)
- **用途**:一次请求内「已登录账号」的只读快照:数据库 `User` 行 + 本次会话的 CSRF 令牌
- **属性**:`user_id` / `username` / `label` / `is_admin`(=`role == "admin"`)
- **不变量**:角色与日历权限判定用 `user` 行配合 `app/accounts.py::calendar_access`,
  本模块不复制权限规则;`csrf_token` 直接来自会话,不落库
- **测试**:`tests/test_family_web.py::test_member_can_open_calendar_and_profile`、
  `tests/test_family_pages.py::test_member_nav_hides_admin_links`

### `NotAuthenticated` / `CsrfFailure` / `Forbidden`

- **位置**:`app/deps.py:69` / `:77` / `:81`
- **用途**:三类鉴权失败的统一异常,由 `app/main.py::_install_exception_pages`
  (`app/main.py:172`)转成响应:
  - `NotAuthenticated`:未登录 —— `/api/*` 返回 **401** `{"error":{"code":"unauthorized"}}`;
    其他路径 **303 → `urls.url("/login")?next=<原路径>`**(前缀跟着当次请求)
  - `CsrfFailure`:CSRF 令牌缺失或不符 —— **403**(API 为 JSON `csrf_failed`,页面为 HTML)
  - `Forbidden`:已登录但权限不足(成员访问管理面、改别人的私有日历)—— **403** `forbidden`
- **参数**:`NotAuthenticated(next_url=...)` 只带站内路径;`Forbidden(message=...)` 中文文案
- **不变量**:异常里不得携带账号口令、会话或令牌明文

### `optional_user` / `require_user` / `require_admin_user`

- **位置**:`app/deps.py:139` / `:151` / `:166`;共同实现 `_load_current`(`app/deps.py:129`)、
  会话读取 `current_session`(`app/deps.py:125`)
- **用途**:
  - `optional_user`:未登录返回 `None`(登录页据此决定要不要跳走),**不过闸门**
  - `require_user`:未登录抛 `NotAuthenticated`,然后经 `_gate_verification`
    (本轮新增的这一步)决定要不要抛 `EmailNotVerified`
  - `require_admin_user`:在 `require_user` 之后判断 `is_admin`,否则抛
    `Forbidden("该功能仅家庭管理员可用")`;管理员在 `needs_email_verification` 里被豁免,
    所以未验证的管理员仍进得去
- **返回**:`CurrentUser` / `None`
- **副作用**:每个请求一次会话解析 + 一次账号主键查询
- **不变量**:会话里带 `user_id`,每次请求都重新读账号,
  因此**账号被删除或停用后现有会话立刻失效**(`_load_current`)
- **测试**:`tests/test_family_web.py::test_admin_only_pages_reject_members`、
  `::test_events_api_requires_login`、`tests/test_family_pages.py::test_member_cannot_use_admin_apis`、
  `tests/test_email_verify.py::test_unverified_member_is_blocked`、
  `::test_unverified_admin_is_not_blocked`

### CSRF 依赖族

- **位置**:`require_csrf`(`app/deps.py:198`)、`require_csrf_admin`(`:216`)、
  `require_csrf_header`(`:225`)、`require_csrf_header_admin`(`:230`);
  候选值提取 `_csrf_candidate`(`:173`),统一校验 `_verify`(`:186`)
- **用途**:所有修改类接口都必须携带与当前会话 `csrf` 字段一致的令牌
  - `require_csrf`:表单字段 `csrf_token` 或请求头 `X-CSRF-Token` 都接受(HTML 表单用);
    通过后经 `_gate_verification`
  - `require_csrf_header`:只认请求头 `X-CSRF-Token`(JSON 接口用);同样经 `_gate_verification`
  - `*_admin`:在校验之后追加管理员判定,非管理员抛 `Forbidden`
  - `require_csrf_any`(`:203`):与 `require_csrf` 同构但**跳过闸门**,见上文
- **返回**:`CurrentUser`
- **错误语义**:无会话 → `NotAuthenticated`;令牌不符 → `CsrfFailure`;
  账号不存在/停用 → `NotAuthenticated`;邮箱未验证 → `EmailNotVerified`;
  非管理员 → `Forbidden`
- **快捷别名**:`UserDep` / `AnyUserDep` / `AdminDep` / `CsrfDep` / `CsrfAnyDep` /
  `CsrfAdminDep` / `CsrfHeaderDep` / `CsrfHeaderAdminDep`(`app/deps.py:243`-`250`)
- **不变量**:管理接口必须**登录 + CSRF + 管理员**三者同时满足;
  `CsrfDep` 只保证「登录 + CSRF + 邮箱已验证」,**不隐含管理员**
- **测试**:`tests/test_family_web.py::test_member_without_csrf_header_is_rejected`、
  `tests/test_api_events.py::test_mutation_requires_csrf`、
  `tests/test_api_settings.py::test_recipient_mutations_require_csrf`、
  `tests/test_agent_tokens.py::test_token_management_requires_csrf`、
  `::test_token_management_requires_login`

### `AgentPrincipal(fingerprint, user_id=None, calendar_id=None)`

- **位置**:`app/deps.py:267`
- **用途**:已通过 Bearer 认证的调用方;`label` 仍是 `agent:<指纹>`,用于限流分桶与审计
- **语义**:`user_id` 是令牌归属账号(决定新建事件的归属与权限);
  `calendar_id` 是令牌固定的落库日历,为空表示用该账号的个人日历
- **不变量**:明文令牌不进入日志或响应;指纹 = `sha256(明文)[:16]`,
  与 `app/token_store.py::fingerprint_of_hash` 一致
- **测试**:`tests/test_family_accounts.py::test_agent_token_carries_scope`

### `_bearer_from(request) -> str`

- **位置**:`app/deps.py:284`
- **用途**:只从 `Authorization` 头取 `Bearer` 令牌(大小写不敏感)
- **返回**:去空白后的明文;没有或格式不对返回空字符串
- **不变量**:绝不从 Cookie 或查询参数取令牌——`/api/agent` 没有 CSRF 面,
  也因此已登录会话不能调用它
- **测试**:`tests/test_api_agent.py::test_requires_bearer_token`、
  `::test_session_cookie_does_not_authorize_agent_endpoint`

### `raise_api_error(status_code, code, message, fields=None) -> None`

- **位置**:`app/deps.py:254`
- **用途**:抛出统一 `{"error": {...}}` 形状的 `HTTPException`,与 `app/errors.py` 一致
- **副作用**:抛异常(不返回)

## 变更(2026-09-29:Agent 令牌)

### `require_agent_token(request, session=Depends(get_db)) -> AgentPrincipal`

- **位置**:`app/deps.py:291`
- **用途**:校验 `Authorization: Bearer <token>`
- **令牌来源取并集**:
  - `.env` 的 `AGENT_API_TOKEN`(逗号分隔):以明文存放,直接 `compare_digest` 比对,
    归属**首位管理员**、`calendar_id` 为空
  - 数据库 `agent_tokens` 表:只存哈希,比对 `sha256(明文) == 已存哈希`,
    按行带出 `user_id` / `calendar_id`
- **返回**:`AgentPrincipal`;指纹均为 `sha256(明文)[:16]`(与
  `app/token_store.py::fingerprint_of_hash` 一致,审计日志 `agent:<指纹>` 可直接对账)
- **404 语义**:两个来源都没有任何令牌时,`/api/agent` 整体返回 404 而不是 401;
  命中数据库里历史无归属的令牌时兜底绑到首位管理员(`app/accounts.py::owner_user`),
  与启动引导的处理一致,旧令牌不失效
- **撤销即时生效**:令牌被撤销/停用后不再出现在启用行里,下一次请求即 401
  (若因此一枚不剩,则回到 404)
- **副作用**:每个 Agent 请求多一次「启用令牌」查询(按行取整条记录,以便带出作用域)
- **测试**:`tests/test_agent_tokens.py::test_db_token_authorizes_agent_requests`、
  `::test_db_token_and_env_token_coexist`、`::test_revoked_token_stops_working_immediately`、
  `::test_revoking_every_token_hides_the_endpoint_again`、
  `tests/test_family_accounts.py::test_legacy_agent_token_is_bound_to_owner`、
  `tests/test_family_mail.py::test_legacy_env_agent_token_belongs_to_owner`

## 新增(模板过滤器)

### `local_time_filter(value, tz_name="UTC", fmt="%Y-%m-%d %H:%M") -> str`

- **位置**:`app/deps.py:46`,注册为模板过滤器 `localtime`(`app/deps.py:62`)
- **用途**:把库里的 **naive UTC** 时间按指定 IANA 时区渲染成给人看的时间
- **参数**:
  - `value`:naive UTC `datetime`;`None` 返回 `—`
  - `tz_name`:IANA 时区名,**非法时退回 UTC 而不抛错**(页面不该因为一条脏配置整页 500)
  - `fmt`:`strftime` 模板,列表页默认到分钟,`title` 属性用到秒
- **返回**:格式化字符串;缺失值为 `—`,与页面其它空值一致
- **模板用法**:
  ```jinja
  {{ delivery.created_at | localtime(default_timezone) }}
  {{ delivery.sent_at | localtime(default_timezone, '%Y-%m-%d %H:%M:%S') }}
  ```
- **不变量**:库内时间一律 naive UTC,换算统一走 `app.timeutil.utc_to_local`,
  不要在模板里做时区算术
- **测试**:`tests/test_timezones.py::test_mail_queue_shows_operation_and_sent_time`

## 变更记录

- 2026-09-29:新增 `local_time_filter` / `localtime` 过滤器,供邮件队列展示操作与发送时间。
- 2026-09-29:`require_agent_token` 增加数据库令牌来源(哈希比对),与 `.env` 取并集。
- 2026-10-08:家庭版新增 `CurrentUser`、`optional_user`/`require_user`/`require_admin_user`、
  `NotAuthenticated`/`CsrfFailure`/`Forbidden` 与四个 CSRF 依赖及其管理员别名;
  `AgentPrincipal` 增加账号与日历作用域,数据库令牌按行带出作用域,历史无归属令牌兜底绑首位管理员。
  同时按当前源码校正了 `require_agent_token`、`local_time_filter` 的行号。
- 2026-10-12:公开前缀与邮箱验证闸门。新增模板全局 `bp`/`url`(取自
  `app/urls.py::current_prefix` / `url`);`is_api_request` 改为前缀感知的
  `urls.is_api_path`;新增 `EmailNotVerified` 异常、`_gate_verification` 闸门、
  `require_any_user`/`require_csrf_any` 与别名 `AnyUserDep`/`CsrfAnyDep`,
  并写明 `require_user` / `require_csrf` / `require_csrf_header` 现在都经闸门、
  管理员与无邮箱账号豁免;按当前源码校正全部行号。