# `app/config.py`

应用配置。所有可配置项都来自环境变量或 `.env`(路径可用环境变量 `SISI_ENV_FILE` 覆盖,
设为不存在的路径即可完全忽略 `.env`,测试就是这么隔离的)。

家庭版新增的对外可见配置只有**派生路径 `feed_dir`**、**公开基地址 `public_base_url`**
与随之纳入 `ensure_directories()` 的目录准备;邮箱验证的三个阈值
(`email_code_ttl_seconds` / `email_code_max_attempts` / `email_code_resend_seconds`)
也走环境变量。其余家庭能力(账号、日历、邀请码、订阅令牌)都落库,不走环境变量。
**敏感值只允许通过环境注入**,绝不写入 SQLite、日志或响应。

## 派生路径(家庭版语义)

| 属性 | 位置 | 路径 | 对外可见 |
|---|---|---|---|
| `data_dir` | `app/config.py:42` | `DATA_DIR`,默认仓库下 `data/` | — |
| `export_dir` | `app/config.py:101` | `<data>/export` | 目录本身不对外 |
| `calendar_path` | `app/config.py:105` | `<data>/export/calendar.ics` | **不对外**;只是全库并集,供版本号/`content_hash`/一致性检查用 |
| `feed_dir` | `app/config.py:110` | `<data>/export/feeds` | **对外**,经 `/calendar/<令牌>.ics` 暴露 `user-<账号id>.ics` 与 `family.ics` |
| `snapshot_dir` | `app/config.py:118` | `<data>/snapshots` | 不对外;版本快照与邮件附件 |
| `backup_dir` | `app/config.py:123` | `<data>/backups` | 不对外 |
| `secret_dir` | `app/config.py:127` | 同 `data_dir` | 不对外;放 `session_secret`、`admin.hash` |
| `db_path` | `app/config.py:97` | `<data>/calendar.db` | 不对外 |
| `database_url` | `app/config.py:131` | `sqlite+pysqlite:///<db_path>` | 不对外 |

- **`feed_dir` 是家庭版新增**:并集文件留在 `export/`(内部一致性产物),
  用户各自的订阅文件放在 `export/feeds/`;`calendar_path` 不再是对外发布路径。
- **不变量**:`/calendar` 系列地址**只能**服务 `feed_dir` 下的文件,
  绝不允许退化成 `calendar_path`(见 [`app-routers-feed.md`](app-routers-feed.md));
  版本号与 `content_hash` 仍由全库并集决定。

### `feed_target_key(*, user_id) -> str` / `feed_path_for(settings, *, user_id) -> Path`

- **位置**:`app/exporter.py:86` / `app/exporter.py:90`
- **用途**:把订阅目标映射成文件名:`user_id` 为空 → `family.ics`,否则 `user-<账号id>.ics`
- **返回**:`feed_dir` 下的路径
- **不变量**:文件名只取决于账号 id,**不含令牌明文或指纹**
- **测试**:`tests/test_family_mail.py::test_feed_files_exist_for_every_target_after_export`

## 家庭版相关的配置项(只写语义,不写密钥)

| 字段(环境变量同名,大小写不敏感) | 位置 | 默认 / 语义 |
|---|---|---|
| `DATA_DIR`(`data_dir`) | `:42` | SQLite、导出、快照、备份、密钥的根目录;`~` 会展开 |
| `SESSION_SECRET`(`session_secret`) | `:51` | 会话签名密钥;**建议只从环境注入**,为空时用 `<data>/session_secret` |
| `SESSION_MAX_AGE_SECONDS` | `:52` | 会话有效期,默认 `12*60*60`;超过即视为未登录 |
| `CALENDAR_NAME` | `:55` | 日历显示名;家庭成员的订阅文件用「`<名字> · <账号标签>`」 |
| `DEFAULT_TIMEZONE` | `:56` | 默认 IANA 时区,与账号自设时区配合(`app/accounts.py::user_timezone`) |
| `PUBLIC_BASE_URL`(`public_base_url`) | `:61` | 对外公开的基地址(如 `https://host/calendar`);**其路径部分就是公开面的挂载前缀**,邀请链接、每人的订阅地址、邮件正文里的地址都从它派生。**取代并删除了旧的 `SUBSCRIPTION_URL`** |
| `FEED_CACHE_SECONDS` | `:62` | 订阅响应 `Cache-Control: max-age`;仍固定带 `private` 与 `must-revalidate` |
| `SMTP_*` | `:65`-`:72` | 邮件配置;`SMTP_PASSWORD` **只从环境注入**,库/日志/响应均不得出现 |
| `EMAIL_CODE_TTL_SECONDS`(`email_code_ttl_seconds`) | `:76` | 验证码有效期(秒),默认 `1800`(30 分钟);登记时实际下限 60 秒(`max(60, …)`) |
| `EMAIL_CODE_MAX_ATTEMPTS`(`email_code_max_attempts`) | `:77` | 单个验证码允许的错误次数,默认 `5`:**第 5 次错误即作废** |
| `EMAIL_CODE_RESEND_SECONDS`(`email_code_resend_seconds`) | `:78` | 两次重发之间的冷却(秒),默认 `60`;配成 `0` 等于不限流 |
| `MAIL_MAX_ATTEMPTS` / `MAIL_BACKOFF_MINUTES` | `:81` / `:82` | 发信重试;退避已在库内持久化 |
| `EXPORT_SNAPSHOT_RETENTION_DAYS` | `:83` | 版本快照保留天数 |
| `SCHEDULER_INTERVAL_SECONDS` / `SCHEDULER_ENABLED` | `:84` / `:85` | 后台任务间隔与开关 |
| `AGENT_API_TOKEN` | `:89` | 逗号分隔的兜底 Agent 令牌;**归属首位管理员**;留空且库里也没有令牌时 `/api/agent` 整段 404 |
| `AGENT_RATE_LIMIT_PER_MINUTE` | `:90` | 单个令牌指纹每分钟的 `/api/agent` 请求上限 |
| `BACKUP_RETENTION_DAYS` | `:93` | 备份保留天数 |
| `LOG_LEVEL` / `HOST` / `PORT` | `:43`-`:45` | 运行参数 |

### 派生布尔与列表属性

| 属性 | 位置 | 语义 |
|---|---|---|
| `backoff_minutes` | `app/config.py:135` | 解析 `MAIL_BACKOFF_MINUTES`;空串回落默认序列 |
| `public_base_path` | `app/config.py:145` | 公开面挂载前缀,取自 `PUBLIC_BASE_URL` 的路径部分(转调 `app/urls.py::public_base_path`);基地址没有路径时为空串,整套路由退回裸路径 |
| `mail_configured` | `app/config.py:152` | 有 `SMTP_HOST` 且有 `SMTP_FROM_ADDRESS` 才为真 |
| `agent_tokens` | `app/config.py:156` | 逗号分隔切分并去空白,返回 `tuple[str, ...]`;明文只存在于进程内存 |
| `agent_enabled` | `app/config.py:162` | 是否配置了 `.env` 令牌(**不代表接口可用**——库里令牌也算) |

### 校验器

- `_expand_data_dir`(`app/config.py:167`):`DATA_DIR` 支持 `~`
- `_check_smtp_security`(`app/config.py:174`):`SMTP_SECURITY` 只接受
  `starttls` / `ssl` / `none`(大小写不敏感),其余抛 `ValueError`

## 函数

### `ensure_directories() -> None`

- **位置**:`app/config.py:181`
- **用途**:创建 `data_dir` / `export_dir` / **`feed_dir`** / `snapshot_dir` / `backup_dir`
- **副作用**:`mkdir(parents=True, exist_ok=True)`(幂等)
- **本次变更**:新增 `feed_dir`
- **不变量**:`secret_dir` 就是 `data_dir`,单列它会让「密钥放在哪」出现两个答案

### `load_or_create_session_secret() -> str`

- **位置**:`app/config.py:192`
- **用途**:返回会话签名密钥:优先 `SESSION_SECRET`;否则在 `<data>/session_secret`
  生成并复用(首次生成记一条 warning),避免每次重启导致登录态失效
- **副作用**:可能创建目录并写文件(尽力 `chmod 0o600`)
- **不变量**:密钥不写日志正文、不进数据库、不回显;
  它同时是**账号固定订阅地址**(`app/feed_tokens.py::account_feed_token`)与
  **验证码哈希**(`app/email_verify.py::code_hash`)的唯一输入——轮换它会一次性
  让所有固定地址与在途验证码失效(`AGENTS.md` 不变量 22、23)

### `public_base_path`(属性)

- **位置**:`app/config.py:145`
- **用途**:给出公开面的挂载前缀(如 `/calendar`)
- **行为**:转调 `app/urls.py::public_base_path(self)`,避免 `config` 与 `urls` 循环导入
- **返回**:规范化的前缀;`PUBLIC_BASE_URL` 只有主机名时返回 `""`
- **不变量**:前缀只有一个来源(`PUBLIC_BASE_URL` 的路径部分),
  不得再引入第二个「订阅挂载点」配置——旧的 `SUBSCRIPTION_URL` 已删除
- **测试**:`tests/test_public_prefix.py::test_public_surface_is_served_under_the_prefix`、
  `::test_base_url_without_path_falls_back_to_bare_paths`

### `env_file_path() -> str`

- **位置**:`app/config.py:218`
- **用途**:读 `SISI_ENV_FILE`;指向不存在的文件等价于「忽略 `.env`」
- **返回**:路径字符串;未设置时返回 `.env`

### `get_settings() -> Settings` / `reset_settings_cache() -> None`

- **位置**:`app/config.py:223` / `app/config.py:228`
- **用途**:带 `lru_cache` 的进程级配置单例;`reset_settings_cache` 供测试清缓存
- **不变量**:`Settings()` 直接构造只读进程环境变量——`env_file` 只在 `get_settings()`
  里显式传入,保证行为可预测

## 测试

- `.env` 隔离:`tests/conftest.py`(测试用 `SISI_ENV_FILE` 指向不存在路径)
- 配置语义:`tests/test_family_accounts.py::test_effective_settings_unchanged_by_accounts`
- 订阅基址(公开基地址 → 挂载前缀与绝对地址):
  `tests/test_public_prefix.py::test_subscription_url_comes_from_public_base_url`、
  `tests/test_family_accounts.py::test_subscription_url_uses_configured_base`、
  `tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`
- 没有路径的基地址退回裸路径:
  `tests/test_public_prefix.py::test_base_url_without_path_falls_back_to_bare_paths`
- 新订阅目录:`tests/test_family_mail.py::test_feed_files_exist_for_every_target_after_export`
- Agent 限流阈值:`tests/test_api_agent.py::test_rate_limit`

## 找不到对应测试的点

- `feed_dir` 的目录创建(`ensure_directories` 新成员)没有专门的断言,
  只有「导出后各目标文件存在」的间接覆盖。
- `email_code_ttl_seconds` / `email_code_max_attempts` / `email_code_resend_seconds`
  三个阈值没有专门的配置用例:它们只按**默认值**被
  `tests/test_email_verify.py` 的行为用例间接覆盖(超时、5 次作废、60 秒冷却),
  改默认值时测试会失败,但改环境变量名的拼写不会。
- `SMTP_PASSWORD` 的「不落库」由 `tests/test_mail.py::test_smtp_password_is_never_persisted`
  覆盖;会话签名密钥(`SESSION_SECRET` / `session_secret` 文件)没有同类测试。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档新增的派生路径 `feed_dir`(及 `ensure_directories`
  纳入该目录)、`calendar_path` 改为内部并集产物,并汇总家庭版相关的配置语义
  (账号/日历/邀请码/订阅令牌均落库,不经环境变量;敏感值只从环境注入)。
- 2026-10-12:`SUBSCRIPTION_URL` **删除**,由 `PUBLIC_BASE_URL`(`:61`)取代——
  它的路径部分就是公开面挂载前缀,并新增派生属性 `public_base_path`(`:145`);
  新增邮箱验证的三个阈值 `EMAIL_CODE_TTL_SECONDS` / `EMAIL_CODE_MAX_ATTEMPTS` /
  `EMAIL_CODE_RESEND_SECONDS`(`:76`–`:78`,默认 1800/5/60);
  `load_or_create_session_secret` 补记它是固定订阅地址与验证码哈希的唯一输入;
  顺带把本文件里因新配置插入而漂移的行号全部对齐到源码。