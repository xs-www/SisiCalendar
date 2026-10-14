# AGENTS.md

面向在本仓库继续开发的自动化代理与人类维护者。

## 项目一句话

家庭多人共享日历:3–5 个账号,每人一条**只有本人可见**的私人日历,外加一条全家共享的日历;
SQLite 是唯一事实来源,按订阅令牌派生只读 ICS 供各人的 iPhone 订阅,每次确认保存后按收件人
范围邮件分发**尚未结束的日程**快照(附件不含已结束的日程)。

设计文档:`docs/本地日程与订阅日历服务-开发文档.md`(需求与验收的最终依据)。
部署运维:`docs/部署与运维.md`。验收证据:`docs/验收测试记录.md`。

## 常用命令

```powershell
uv sync                                   # 安装依赖(需要 uv)
uv run alembic upgrade head               # 手动迁移:url 写死在 alembic.ini,固定 ./data/calendar.db
                                          # 它**不读 DATA_DIR**,别在测试副本上用它(会打到真实库)
uv run uvicorn app.main:app --reload      # 本机开发
uv run pytest -q                          # 全量测试
uv run ruff check app tests --fix         # 静态检查
uv run python -m app.cli verify           # 一致性检查 + 自动修复
uv run python -m app.cli backup           # 生成一致的 SQLite 快照
uv run python -m app.cli bootstrap        # 查看/轮换首位管理员的引导码(口令救援,不需要口令)
uv run python -m app.cli reset-admin-password --generate   # 口令彻底丢失时重置(打印一次性口令)
```

`DATA_DIR` 默认是仓库下的 `data/`,开发时可用环境变量指向别处。测试全部使用临时目录。

## 代码地图

```text
app/
├─ main.py          应用装配、lifespan、安全响应头、异常页
├─ config.py        环境配置与派生路径(唯一配置入口)
├─ db.py            引擎、会话、SQLite PRAGMA(WAL)
├─ models.py        全部表定义(账号、日历、成员、邀请码、订阅令牌、事件、收件人、令牌)
├─ schemas.py       请求/响应模型
├─ deps.py          模板环境、当前用户与 CSRF 依赖、Agent 令牌鉴权
├─ security.py      Argon2id 口令、签名会话、CSRF、限流、审计日志
├─ accounts.py      账号、私人/共享日历、成员角色与 CalendarAccess 可见可写判定
├─ invites.py       邀请码(只存哈希,次数/有效期,注册)
├─ feed_tokens.py   ICS 订阅令牌(只存哈希,按账号或家庭范围)
├─ email_verify.py  邮箱验证码(只存哈希,TTL/次数/冷却)与两封通知信
├─ urls.py          公开面路径前缀:请求级前缀绑定、绝对地址与邀请/订阅链接
├─ errors.py        统一 {"error": {...}} 响应
├─ timeutil.py      时间换算(库内一律 naive UTC)
├─ timezones.py     常用时区清单与序号解析
├─ recurrence.py    RRULE 解析/构造/表单互转
├─ reminders.py     提醒取值、预设与中文描述
├─ events.py        事件校验、查询、重复事件展开、序列化
├─ ics.py           RFC 5545 导出(含 VALARM)
├─ exporter.py      原子替换、版本状态、按目标导出、快照、一致性检查
├─ sync_service.py  「保存 → 生成 → 建邮件任务」编排(publish 是唯一入口)
├─ mailer.py        SMTP 连接与 MIME 构造
├─ mailqueue.py     持久化发信队列、幂等、退避重试、按收件人范围取附件
├─ settings_store.py 非敏感配置覆盖(含对外基地址的 origin 覆盖)
├─ host_port.py     宿主端口映射的只读探测(设置页「检测宿主端口映射」用)
├─ token_store.py   远程接入令牌(只存 sha256,明文只在创建时返回一次)
├─ scheduler.py     后台任务(ICS 修复、队列轮询、快照清理)
├─ backup.py        在线备份、恢复、清理
├─ bootstrap.py     迁移与启动检查(家庭版数据引导)
├─ bootstrap_access.py 首位管理员的引导码(口令救援通道,文件载体)
├─ cli.py           运维命令
└─ routers/         health / feed / auth / pages / api_*
```

## 必须保持的不变量

改动前请确认不会破坏以下约束,它们都有对应测试:

1. **时间一律以 naive UTC 落库**;输入带偏移量按偏移解释,不带偏移量按事件的 IANA 时区解释。
   展示与 ICS 生成时再还原。见 `test_naive_input_uses_event_timezone`。
2. **UID 创建后永不变更**;修改才递增 `sequence`,无实际变化不递增、不产生新版本。
   见 `test_uid_is_stable_across_exports`、`test_noop_update_does_not_create_new_version`。
3. **导出内容必须只取决于数据库状态,不能取决于导出时刻。**
   导出器用内容哈希判断是否需要新版本,所以 `DTSTAMP` 等字段一律取事件自身的修订时间
   (`updated_at`),绝不能取挂钟时间。否则跨秒重新导出会得到新哈希,导致没有改动的保存
   也递增版本、并向所有订阅端与收件人重发一遍。见 `test_content_is_deterministic_across_wall_clock`、
   `tests/test_family_mail.py::test_target_files_are_deterministic_across_wall_clock`。
4. **发布文件只做原子替换**:先写同目录临时文件并 `fsync`,再 `os.replace`。
   订阅端只能读到完整的旧文件或新文件。见 `test_atomic_replace_never_exposes_partial_file`。
5. **导出失败不得丢数据**:事件与 `dirty` 先提交,再生成 ICS;失败时回滚导出事务并
   单独补记 `last_error`,订阅端继续返回最近的有效文件。见 `test_feed_serves_last_valid_file_when_export_fails`。
6. **邮件任务幂等键是 `(export_version, recipient_id)`**,唯一约束在数据库层;
   附件永远取自该任务记录的不可变快照,重试不得改用更新版本。
   附件内容是**该版本中尚未结束的事件**,按收件人的目标落盘:
   `snapshots/calendar-v{N}-user-<账号id>-upcoming.ics`(账号收件人 → 他的可见范围)与
   `snapshots/calendar-v{N}-family-upcoming.ics`(外部收件人 → 只有共享日历);
   含正在进行与仍在重复的事件,已经结束的一律排除,当天早些时候结束的也算,
   所以 `attachment_hash` 通常不等于 `content_hash`;
   **按挂钟时间过滤只允许发生在这些邮件专用快照上**,发布文件与版本快照必须保持完整,
   否则就破坏了不变量 3。见 `tests/test_mail.py`、`tests/test_family_mail.py`。
7. **SMTP 口令只来自环境变量**,不写数据库、不写日志、不回显;日志中的邮箱一律脱敏
   (`app/util.mask_email`)。见 `test_smtp_password_is_never_persisted`。
8. **管理接口必须登录 + CSRF,且区分管理员**:`/admin/members`、`/admin/settings`、`/admin/sync`
   与 `/api/v1/settings`、`/api/v1/sync/*`、`/api/v1/sync-recipients` 只对 `admin` 开放
   (成员访问 403);成员可用的写接口只有自己的资料/口令/订阅令牌与自己可写日历里的事件;
   `/calendar/<令牌>.ics` 只有 GET/HEAD 且公开;`/calendar`(前缀本身)与 `/calendar.ics`
   **不再提供日历**,一律纯文本 404。
   **公开面的路径前缀来自 `PUBLIC_BASE_URL` 的路径部分**(默认 `/calendar`),所有链接都由
   `app/urls.py` 按**当次请求**绑定的前缀渲染:从 `/calendar/login` 进来的页面里全是
   `/calendar/...`,本机 `http://localhost:443/admin` 进来的仍是裸路径。见 `tests/test_public_prefix.py`。
9. **全天事件的 `end_date_exclusive` 不含在事件内**;ICS 里 `DTEND` 是同义的不含边界日期。
10. **默认时区相关查询**:`GET /api/v1/events` 只有显式给了 `start`/`end` 才做日期过滤,
    否则关键词检索应覆盖全部事件。
11. **远程 Agent 接口(`/api/agent`)**只认 `Authorization: Bearer`,不接受 Cookie 会话
    (因此没有 CSRF 面,也意味着已登录会话不能调用它);令牌有两个来源并**取并集**——
    `.env` 的 `AGENT_API_TOKEN` 与数据库 `agent_tokens` 表;两个来源都没有令牌时整段返回 404
    而不是 401,避免暴露接口存在;`client_ref` 命中时必须原样返回既有事件,不得新建、
    不得重新导出、不得发信。见 `tests/test_api_agent.py`。
12. **远程接入令牌只存 `sha256` 哈希**:明文仅允许出现在**创建成功那一次响应**里,
    之后不得回显;库、日志、审计 detail 中都不允许出现明文,页面只展示指纹
    (哈希前 16 位,与审计日志 `agent:<指纹>` 一致)。见 `tests/test_agent_tokens.py`。
    每枚令牌绑定一个账号(`user_id`)与一条默认日历(`calendar_id`);历史无归属的令牌在引导时
    绑到首位管理员,请求时也兜底按首位管理员解释。
13. **隐私优先的可见性判定**:私人日历(`calendars.kind='personal'`)只有归属账号可读可写,
    **管理员也不能读别人的私人日历**(读 404、写 403);一切读接口都必须经过
    `CalendarAccess.viewable_ids()`/`can_view()` 过滤,写接口必须经过 `can_write()`,
    越界事件在列表里根本不出现、按 id 读取 404。见 `tests/test_family_accounts.py`、
    `tests/test_family_web.py`。共享日历写入仅限 `owner`/`editor`,新成员默认 `viewer`。
14. **订阅文件的范围**:`/calendar/<令牌>.ics` 只服务该令牌账号可见日历的合并结果
    (`export/feeds/user-<id>.ics`);`user_id` 为空的**家庭令牌**只服务共享日历
    (`export/feeds/family.ics`)。**无令牌的旧地址已经取消**:`/calendar` 与 `/calendar.ics`
    一律纯文本 404(前缀本身对日历客户端回 404、对浏览器 303 到管理页),
    它们**绝不能**退化成全库并集,也不能再退回「首位管理员的可见范围」。
    每个账号还有一条**由服务端密钥派生、永不变更**的固定地址(见不变量 23)。
    见 `tests/test_family_web.py::test_owner_fixed_address_serves_owner_scope_not_union`、
    `::test_legacy_tokenless_address_is_gone`、`tests/test_public_prefix.py`。
15. **版本号与内容哈希仍由全库并集决定**:`export/calendar.ics` 只是内部一致性产物
    (版本、`content_hash`、`verify_consistency` 用),不得对外暴露;任何成员的改动都会推进
    全局 `export_version`。`resolve_targets()` 只包含**启用**账号 + 家庭目标;停用账号必须
    从导出与邮件目标里消失。见 `tests/test_family_mail.py`。
16. **邮件范围按收件人区分**:收件人的「身份」与「范围」是两件事——
    `sync_recipients.user_id` 非空 = 该账号的**账号收件人**(由 `users.email` 派生,每个账号最多一行);
    `scope_user_id` = 这封邮件按**谁的可见范围**裁剪(为空 = 家庭共享日历),统一由
    `app/accounts.py::recipient_scope_user_id()` 解释。账号收件人只收到自己可见日历的快照,
    普通外部收件人只收到家庭共享日历,单用户版升级上来的历史邮箱保持
    `scope_user_id = 首位管理员`、`user_id` 为空(收信范围与升级前完全一致)。
    正文里**不得**出现别人的订阅地址:账号收件人收到的是**他自己**那条固定地址,
    外部邮箱(含历史邮箱)一条地址都不给,只提示地址在各自账号的「我的设置」里,
    并集地址与旧 `SUBSCRIPTION_URL` 已经彻底取消。见 `tests/test_family_mail.py`。
17. **账号邮箱只能通过账号资料维护**:`users.email` 是账号收件人的唯一来源,外部收件人接口
    拒绝改名、启停或删除账号行(409);清空账号邮箱会删除对应收件人,停用账号会停掉该行
    (重新启用账号即自动恢复)。见 `tests/test_family_web.py`、`tests/test_family_pages.py`、
    `tests/test_account_recipient_sync.py`。
18. **邀请码与订阅令牌同样只存 `sha256`**:明文只在创建响应里出现一次,列表与页面只显示指纹;
    邀请码用尽/停用/过期后不可再注册(`unusable_reason` 先判次数再判停用),
    订阅令牌撤销后地址立刻 404。见 `tests/test_family_accounts.py`。
    **唯一的例外是账号固定地址**(不变量 23):它的明文可以由服务端重新算出来,所以页面可以
    长期显示;但数据库里仍然只有 `token_hash`,任何地方都不得把明文写进日志或审计。
19. **引导必须幂等**:`ensure_family_setup` 在已有家庭数据时不得产生任何副作用;结构变更只写在
    Alembic 迁移里,数据回填(首位管理员、私人/共享日历、成员关系、历史事件与历史收件人/令牌的
    归属)只发生在引导里。见 `tests/test_family_accounts.py::test_bootstrap_*`。
20. **删除账号必须在应用层清空归属**:SQLite 的 `batch_alter_table` 加列**不带外键**,由迁移
    升级上来的库里 `events.created_by_user_id` 等列没有 `ON DELETE SET NULL`,所以
    `app/accounts.py::delete_user` 必须显式删除该账号的个人日历与其中的事件,并把剩余事件的
    `created_by_user_id` 置空——不要在删除路径上依赖数据库外键(两条建库路径的行为必须一致)。
21. **收件人的身份与范围必须分开**:`sync_recipients.user_id` 是「这行属于哪个账号」的身份标记,
    **每个账号最多一行**(账号收件人);`scope_user_id` 是收信范围,可以指向任何账号。
    引导、迁移与 `sync_user_recipient()` 都不得把多个邮箱写进同一个 `user_id`——真机升级时
    正是这个写法让 `scalar_one_or_none()` 抛 `MultipleResultsFound`,容器进入重启循环。
    取「唯一一行」的地方要么保证唯一(靠迁移 0006 的修复),要么像 `sync_user_recipient()` 那样
    容忍重复(告警 + 按 `users.email` 匹配 + 认不出就一行都不删)。见
    `tests/test_family_upgrade_recipients.py`。
22. **邮箱必须验证过才算数**:非管理员账号**有邮箱但没验证**时进不了任何业务页面与接口
    (`EmailNotVerified`:页面 303 到验证页、`/api` 403 `email_not_verified`),也不能成为邮件收件人
    ——`sync_user_recipient()` 只认「有邮箱 **且** 已验证」的账号。验证码 6 位、30 分钟有效、
    最多错 5 次(第 5 次直接作废)、重发冷却 60 秒;改动邮箱会立刻把账号打回待验证并重发验证码;
    验证通过后补发一封「你的固定订阅地址」邮件。**首位管理员豁免**(本机救急不能被自己的邮箱
    锁在门外),没有邮箱的成员只是被提示、不受阻。验证码与固定地址都不进日志:库里只有
    HMAC 哈希,邮件正文才出现明文。见 `tests/test_email_verify.py`。
23. **每个账号的固定订阅地址由服务端密钥派生、永不变更**:
    `token = sisi_feed_ + HMAC(session_secret, "feed:<user_id>")[:40]`,账号创建时(含引导回填)
    就登记一行 `feed_tokens`(标签「账号订阅地址(固定)」,库里只存 sha256),界面不给停用/撤销
    (409),但地址可以随时重新显示、重新发信。**轮换 `data/session_secret` 会让所有固定地址失效**
    (同时让所有验证码哈希失效),这是刻意的取舍:密钥即身份。
    见 `tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`、
    `tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`。
24. **账号收件人是派生数据,「设置里的同步邮箱」只碰外部邮箱**:
    `sync_recipients` 里 `user_id` 非空的行完全由
    `users.email` + `users.enabled` + `email_verified_at` 决定,由
    `app/accounts.py::sync_account_recipients()` 对齐(启用且已验证 → 一条**启用**的账号收件人;
    账号停用 → 该行 `enabled=False`,不删行)。它必须在**三个时机**被调用:启动引导、
    **建发信任务之前**(`app/mailqueue.py::ensure_delivery_tasks`,不依赖上一次启动是否修好)、
    以及设置页渲染前。设置页(`/admin/partials/recipients` 的 toggle/delete)与
    `/api/v1/sync-recipients` 的写操作只允许作用于外部邮箱,**命中账号行一律 409**
    (页面此前没有守卫,管理员能把成员的账号收件人删掉/停用,成员就再也收不到同步邮件)。
    要停止某个账号收信,唯一做法是停用该账号。见 `tests/test_account_recipient_sync.py`。
25. **一次改动只通知「看得见它」的收件人**:具体日程的增删改会把改动落在的日历
    (`changed_calendar_ids`,PATCH 换日历时是**新旧两条**)交给
    `app/sync_service.py::publish` → `ensure_delivery_tasks`;后者用
    `_scope_calendar_ids()`(与附件同一套范围判定)与它求交集,**没有交集就不建发信任务**
    ——否则收件人会收到一封附件里根本没有这条日程的邮件。手动群发(重新生成、
    「向当前人员发送最新版」)、CLI 导出与后台修复拿不到具体日历,留空即「发给全部收件人」;
    `publish` 在调用方没给集合时按 `event.calendar_id` 推断。见
    `tests/test_account_recipient_sync.py`。
26. **首位管理员的引导码只在「初始口令」状态下存在**:
    `app/bootstrap_access.py` 把码写成 `${DATA_DIR}/bootstrap_code`(权限 600,`sisi_boot_` 前缀),
    判据是 `users.must_change_password`;`ensure_family_setup` 每次启动按状态对齐
    (待改密 → 确保文件在,手工删了会自愈;已定稿 → 删掉)。**口令一设置成功
    (`set_password(..., must_change=False)`)文件立刻删除、`/bootstrap` 立刻 404**——
    它不是常驻后门。引导码**永不**进日志、数据库、邮件或审计(日志只写文件路径),
    比较必须常量时间**且先把两侧编码成字节**(`hmac.compare_digest` 对非 ASCII 的 `str` 会抛
    `TypeError`,那会把「码错」变成 500)。救援入口是 `app.cli bootstrap`(查看/`--rotate`)与
    `app.cli reset-admin-password`;`.env` 里的 `ADMIN_PASSWORD_HASH` **只在首次创建首位管理员时
    生效**,对已有库改它无效。见 `tests/test_bootstrap_access.py`。
27. **对外基地址的 origin 可以被库覆盖,路径前缀不可以**:
    `settings_store.PUBLIC_BASE_URL_KEY` 在库里**只存 origin**(`https://主机[:端口]`),
    完整地址由 `compose_public_base_url` 与**环境变量 `PUBLIC_BASE_URL` 的路径部分**现拼
    (所以以后改 `.env` 重建容器也不会带错路径);校验拒绝一切非 origin 形态,包括
    「路径与挂载前缀不一致」。理由:路径前缀是**启动时挂载路由**用的(`app/main.py`),
    运行时改它会让页面链接与路由错位。按请求推导 tailnet 候选地址时,`Host` /
    `X-Forwarded-Proto` **只作为建议来源、不是安全判据**,只对 `*.ts.net` 生效,且必须管理员
    确认保存后才写库;宿主端口探测**只能**连 `host.docker.internal:<HOST_PORT>/healthz`
    (主机名写死、端口只认 1–65535 的整数,不是任意 URL 探测器)。
    见 `tests/test_settings_public_base_url.py`。

## 部署时的三个陷阱

- **对外只挂 `/calendar` 一段**(外加 DSH 自己的 `/`、`/dsh` 与 Bearer-only 的 `/agent`):
  成员的登录、注册、管理界面都在 `/calendar/login`、`/calendar/register`、`/calendar/admin/...`,
  每人的订阅地址是 `/calendar/<令牌>.ics`。**不要再挂裸 `/login`、`/logout`、`/register`、`/admin`、
  `/api`、`/static`**:那样会把管理面暴露到整个 tailnet。裸路径仍然在**本机**可用
  (`http://localhost:443/admin`),给管理员在机器上排障用;成员一律走带前缀的地址。
  权限边界是账号 + 角色,不是网络隔离:`/admin/members`、`/admin/settings`、`/admin/sync`
  与相关 API 对成员仍 403。
- **挂载路径必须与后端路径逐字一致,而且要和 `PUBLIC_BASE_URL` 的路径部分一致**:
  应用里的链接按当次请求的前缀渲染(见不变量 8),绝对地址(邀请链接、订阅地址、邮件正文)
  一律取自 `PUBLIC_BASE_URL`。本仓库的用法**不依赖前缀剥离**:`--set-path=/calendar` 配
  `http://127.0.0.1:443/calendar`,外部的 `/calendar` 与后端的 `/calendar` 是同一个入口。
  改完务必确认 `/calendar/login` 仍 200、`/calendar` 本身是 404 纯文本(说明订阅命名空间没被
  管理页污染、旧的无令牌地址也真的取消了)。详见《部署与运维》第 3 节。
- **Docker NAT 让本机请求与代理请求同源**:容器内看到的对端都是 Docker 网关(`172.19.0.1`),
  应用层无法区分「本机浏览器」与「经 Tailscale 来的请求」,因此不要指望靠客户端地址做管理面隔离,
  判定一律走账号与角色。

## 约定

- 所有用户可见文案与注释使用简体中文;代码标识符使用英文。
- 数据库结构变更必须新增 Alembic 迁移(`migrations/versions/`),不要修改已应用的迁移。
- `alembic.ini` 必须保持纯 ASCII:它以系统 locale 编码读取,中文会导致 `UnicodeDecodeError`。
- 修改行为时同步更新测试与 `docs/验收测试记录.md` 中的对应条目。
- 新增敏感字段时先问:它会不会进入日志、数据库或镜像?参照第 6 条处理。
- **写进文档的真机证据必须脱敏**:`docs/`、`README.md`、`AGENTS.md`、`todo.md` 都是
  **纯文本产物,没有任何机制替我们兜底**(日志有 `app/util.mask_email`、库有 SQLite,
  文档只有这条约定)。从生产库粘贴证据时,邮箱一律写成 `mask_email` 的形态
  (`9******@qq.com`),口令、令牌、邀请码、验证码、固定订阅地址、引导码(`bootstrap_code`)
  只写**指纹或掩码**,不写明文;确需指代某人时用显示名或用户名。仓库是公开的,推送即公开且
  **难以撤回**。
  反面案例:2026-10-14 发现第五轮的验收记录直接粘贴了 `sync_recipients` 的真实邮箱明文
  (同日已打码,但那两个地址已经公开过一次)。
- 前端不使用内联脚本:管理页启用了严格 CSP(`script-src 'self'`),JS 放在 `static/js/`,
  需要传给前端的值通过 `data-*` 属性传递。
