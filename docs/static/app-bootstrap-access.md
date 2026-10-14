# `app/bootstrap_access.py`(首位管理员引导码)

本模块是首位管理员「初始口令」救援通道的**唯一实现**:引导码状态、生成、读取、轮换、
校验,以及它与 `users.must_change_password` 的对齐,全部集中在这里。
浏览器侧入口是 `app/routers/auth.py` 的 `GET /bootstrap` / `POST /bootstrap`
(见 [`app-routers-auth.md`](app-routers-auth.md)),CLI 侧入口是
`app/cli.py::cmd_bootstrap` / `cmd_reset_admin_password`(见 [`app-cli.md`](app-cli.md)),
模板是 `templates/bootstrap.html`(见 [`templates.md`](templates.md))。

**背景(真机事故)**:首位管理员的口令若由 `app/security.py::ensure_admin_credential`
**随机生成**,明文只在启动日志里出现过一次;`data/admin.hash` 与 `users.password_hash`
里都只有 Argon2id 哈希,**丢了就再也算不出来**。`docker logs` 一卡,初始口令永久丢失,
除了重置数据库没有别的路。本模块把这条通道换成**一个文件**:
`<DATA_DIR>/bootstrap_code`(权限 600)——宿主机上 `./data/bootstrap_code`、
容器内 `cat /data/bootstrap_code`,随时可读,不以日志为唯一载体。

常量用「取值」代替「参数 / 返回 / 副作用」三个字段(它们对常量没有意义)。

## 常量

### `BOOTSTRAP_FILENAME`

- **位置**:`app/bootstrap_access.py:50`
- **取值**:`"bootstrap_code"`
- **用途**:引导码文件名;它落在 `settings.secret_dir`(即 `app/config.py:131`–`:132`
  返回的 `data_dir`)的**根**,也就是 `<DATA_DIR>/bootstrap_code`
- **不变量**:文件名只在 `code_path()` 里拼一次,别处不得再拼;改这个名字会让**老部署上
  已有的码文件失联**(下次启动当成没有码、重新生成一枚,旧文件留在盘上),
  它与 `data/session_secret`、`data/admin.hash` 是同一个目录下的三个独立文件
- **测试**:`tests/test_bootstrap_access.py::test_code_file_follows_the_initial_password_state`、
  `::test_first_boot_with_a_random_password_writes_the_code_file`
- **变更记录**:2026-10-14:新增。

### `BOOTSTRAP_CODE_BYTES`

- **位置**:`app/bootstrap_access.py:52`
- **取值**:`24`(`secrets.token_urlsafe(24)` → 约 32 个 URL 安全字符)
- **用途**:引导码的熵,即前缀之后那一截随机字节的字节数
- **不变量**:不得下调——`POST /bootstrap` 的限流只有 5 次 / 600 秒,
  真正的防线是这 24 字节随机;它是**模块私有**常量,`__all__`
  (`app/bootstrap_access.py:144`–`:156`)里**没有**它
- **测试**:`tests/test_bootstrap_access.py::test_code_is_random_and_rotation_invalidates_the_old_one`
  (只断言随机性与轮换失效,不依赖具体长度)
- **变更记录**:2026-10-14:新增。

### `BOOTSTRAP_CODE_PREFIX`

- **位置**:`app/bootstrap_access.py:54`
- **取值**:`"sisi_boot_"`
- **用途**:肉眼可识别的前缀:管理员看到它就知道「这就是引导码」,与库里的哈希无关
- **不变量**:前缀**不是秘密**,不得只凭前缀判定有效性(判定永远走 `verify_code()`);
  它只是给人看的标签——**整串引导码**的明文出口仍然只有文件与管理员主动发起的
  stdout / 表单预填两处(见下文不变量 (a))
- **测试**:`tests/test_bootstrap_access.py::test_code_file_follows_the_initial_password_state`
  (断言 `code.startswith(BOOTSTRAP_CODE_PREFIX)`)
- **变更记录**:2026-10-14:新增。

### `BOOTSTRAP_QUERY`

- **位置**:`app/bootstrap_access.py:56`
- **取值**:`"code"`
- **用途**:引导地址里的查询参数名(`bootstrap_url()` 拼成 `?code=…`)
- **不变量**:必须与三处逐字一致——`bootstrap_url()` 的输出、`GET /bootstrap` 的
  `code` 形参(`app/routers/auth.py:646`)、`templates/bootstrap.html:23` 的
  `<input type="text" name="code">`;它是 `app/invites.py::INVITE_PARAM` 的同名镜像
  (两处各留一份,避免循环导入)
- **测试**:`tests/test_bootstrap_access.py::test_bootstrap_url_is_built_from_the_public_base`
  (地址里是 `?code=`)、`::test_bootstrap_page_serves_the_form_with_csp`
  (URL 上带的码被预填进表单)
- **变更记录**:2026-10-14:新增。

## 函数

### `code_path(settings) -> Path`

- **位置**:`app/bootstrap_access.py:59`
- **用途**:给出引导码文件的位置(只算路径,不碰磁盘)
- **参数**:`settings` 只用到 `settings.secret_dir`(= `data_dir`)
- **返回**:`Path`,形如 `<DATA_DIR>/bootstrap_code`;文件存不存在**不在这里判断**
- **副作用**:无
- **不变量**:它是引导码文件路径的**唯一来源**——`bootstrap.py` 的启动日志、路由的页面
  上下文、CLI 的 JSON 都调它,任何地方都不得再手拼一次文件名
- **测试**:`tests/test_bootstrap_access.py::test_code_file_follows_the_initial_password_state`、
  `::test_first_boot_with_a_random_password_writes_the_code_file`
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap.py:201`(启动日志里的路径)、`app/routers/auth.py:637`
  (模板上下文 `code_file`)、`app/cli.py:198`(JSON 字段 `code_file`)

### `read_code(settings) -> str | None`

- **位置**:`app/bootstrap_access.py:64`
- **用途**:读取当前引导码(只读,不生成)
- **参数**:无(读 `code_path(settings)`)
- **返回**:文件内容去掉首尾空白;文件不存在、读不动或内容为空 → `None`
  (空文件与「没有码」是同一个语义)
- **副作用**:无
- **不变量**:任何异常都吞成 `None`(捕获 `OSError`),**不得**把文件内容写进日志或异常信息
- **测试**:`tests/test_bootstrap_access.py::test_first_boot_with_a_random_password_writes_the_code_file`、
  `::test_setting_the_password_closes_the_channel`(设置成功后必须读到 `None`)、
  `::test_wrong_code_and_bad_password_keep_the_channel_open`(失败时码仍在)
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap_access.py:76`(`ensure_code` 的复用分支)、`:103`
  (`verify_code` 取期望值)

### `ensure_code(settings, *, rotate=False) -> str`

- **位置**:`app/bootstrap_access.py:73`
- **用途**:确保存在引导码并返回它;`rotate=True` 时重新生成,旧码立即失效
- **参数**:`rotate` 是**关键字参数**,默认 `False`;只由
  `app/cli.py::cmd_bootstrap --rotate`(`app/cli.py:190`)传 `True`
- **返回**:当前引导码明文(已存在则原样返回,不换码)
- **副作用**:写 `<DATA_DIR>/bootstrap_code`(先 `settings.ensure_directories()`,
  再 `write_text`),并 `chmod 0o600`(`OSError` 被 `contextlib.suppress` 吞掉,
  兼容平台差异);**直接覆盖写,不是原子替换**——它是一次性救援凭证,
  不适用订阅产物的原子替换不变量(`AGENTS.md` 不变量 4)
- **不变量**:`rotate=False` 时**必须先读后写**:已有码一律复用,否则每次启动都会换码,
  管理员抄下来的码当场作废;同一枚码可以长期有效,直到口令被设置
- **测试**:`tests/test_bootstrap_access.py::test_code_is_random_and_rotation_invalidates_the_old_one`
  (不轮换返回同一枚、轮换后旧码失效)、`::test_bootstrap_page_serves_the_form_with_csp`
  (页面渲染前的自愈)、`::test_cli_bootstrap_prints_the_code_and_url`
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap_access.py:134`(`sync_code_state`)、
  `app/routers/auth.py:655`(`GET /bootstrap` 渲染前的自愈)、
  `app/cli.py:190`(`bootstrap`)、`app/cli.py:237`(`reset-admin-password` 后仍待改密时保码)

### `clear_code(settings) -> bool`

- **位置**:`app/bootstrap_access.py:88`
- **用途**:删除引导码文件,关闭救援通道
- **参数**:无
- **返回**:真的删掉了 → `True`;文件本来就不在(或删不掉)→ `False`
- **副作用**:删除磁盘文件;删不掉时写一条 `warning` 日志,**只记路径、不记内容**
  (`app/bootstrap_access.py:97`)
- **不变量**:**口令设置成功的那一刻必须调用它**(`app/routers/auth.py:721`),
  否则 `/bootstrap` 会继续接受旧码;`FileNotFoundError` 是正常路径(幂等),
  不得升级成错误
- **测试**:`tests/test_bootstrap_access.py::test_setting_the_password_closes_the_channel`
  (文件被删、页面 404)、`::test_code_file_follows_the_initial_password_state`
  (经 `sync_code_state` 间接覆盖)
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap_access.py:132`(`sync_code_state`)、
  `app/routers/auth.py:721`(设置口令成功)、`app/cli.py:239`(重置口令后已定稿)

### `verify_code(settings, candidate) -> bool`

- **位置**:`app/bootstrap_access.py:101`
- **用途**:常量时间比较引导码
- **参数**:`candidate` 接受任意对象(先 `str(...)`,再 `strip()`);**不得**假设它一定是字符串
- **返回**:与文件里的码完全一致 → `True`;任一侧为空、或不一致 → `False`
- **副作用**:无(只 `read_code`,不生成、不删除)
- **不变量**:必须用 `hmac.compare_digest`(与 CSRF、令牌比较一致),不得退化成 `==`;
  校验失败**不得**消耗或轮换引导码——码只有 `ensure_code(rotate=True)` 与
  「口令设置成功」两条路会失效(见下文的已知边界:非 ASCII 候选会抛 `TypeError`)
- **测试**:`tests/test_bootstrap_access.py::test_code_is_random_and_rotation_invalidates_the_old_one`
  (正确/加一个字符/空串/`None`)、`::test_wrong_code_and_bad_password_keep_the_channel_open`
  (错码 401 且码仍在)
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/routers/auth.py:704`(`POST /bootstrap`)

### `pending_owner(session) -> User | None`

- **位置**:`app/bootstrap_access.py:110`
- **用途**:给出「仍在用初始口令」的首位管理员
- **参数**:`session` 由调用方提供(路由用请求会话,CLI 用 `session_scope()`)
- **返回**:首位管理员(`app/accounts.py::owner_user`)**且** `must_change_password` 为真 →
  该 `User`;否则(没有账号,或已改过口令)→ `None`
- **副作用**:无(只读)
- **不变量**:判据**只有** `users.must_change_password` 这一个字段——**不要**引入状态文件、
  时间戳或「文件存在与否」作为第二判据;任何路径改过口令都会经
  `app/accounts.py::set_password` 把它清掉,通道随之关闭
- **测试**:`tests/test_bootstrap_access.py::test_code_file_follows_the_initial_password_state`、
  `::test_bootstrap_page_is_closed_when_the_password_is_not_initial`、
  `::test_closed_channel_rejects_the_post_too`、
  `::test_cli_bootstrap_reports_not_pending`
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap_access.py:131`(`sync_code_state`)、
  `app/routers/auth.py:652`(`GET /bootstrap` 的 404 判据)与 `:700`(`POST /bootstrap`)、
  `app/cli.py:175`(`bootstrap`)与 `:236`(`reset-admin-password`)

### `sync_code_state(session, settings) -> str | None`

- **位置**:`app/bootstrap_access.py:122`
- **用途**:让引导码文件与「是否仍处于初始口令」保持一致
- **参数**:`session` + `settings`(后者决定文件位置)
- **返回**:仍处于初始口令 → 当前引导码;已设过口令 → `None`
- **副作用**:仍处于初始口令时**创建**文件(被手工删掉也会在下次启动补回来,即自愈);
  已设过口令时**删除**文件(例如从备份恢复了一个已改过口令的库,不能让旧引导码继续有效)
- **不变量**:它是「状态 → 文件」的唯一同步点,由
  `app/bootstrap.py::ensure_family_setup` 在结尾 `session.commit()` 之后调用
  (`app/bootstrap.py:197`),**幂等**:连跑多次不换码;两次调用之间不得出现
  「文件在但状态已关闭」或「状态待改密但文件不在」的窗口
- **测试**:`tests/test_bootstrap_access.py::test_code_file_follows_the_initial_password_state`
  (待改密 → 有码;改过 → 无码;再同步不换码)、
  `::test_first_boot_with_a_random_password_writes_the_code_file`(经 `ensure_family_setup`)
- **变更记录**:2026-10-14:新增。

### `bootstrap_url(settings, code) -> str`

- **位置**:`app/bootstrap_access.py:137`
- **用途**:给管理员复制粘贴用的**完整**引导地址:浏览器打开即进设置口令页
- **参数**:`code` 是引导码明文,经 `urllib.parse.quote` 转义后放进查询串
- **返回**:`{PUBLIC_BASE_URL}/bootstrap?code={引导码}`,形如
  `https://<节点>/calendar/bootstrap?code=sisi_boot_…`;基地址为空时退回
  `/bootstrap?code=…`(纯相对路径)
- **副作用**:无
- **不变量**:基地址一律取自 `app/urls.py::public_base_url(settings)`
  (`PUBLIC_BASE_URL`,不跟当前请求的前缀走),否则复制出去的地址对家人不可用;
  参数名固定取 `BOOTSTRAP_QUERY`
- **测试**:`tests/test_bootstrap_access.py::test_bootstrap_url_is_built_from_the_public_base`
  (断言完全等于 `https://example.test/calendar/bootstrap?code=sisi_boot_demo`)、
  `::test_cli_bootstrap_prints_the_code_and_url`(JSON 里的 `url` 以 `/bootstrap?code=<码>` 结尾)
- **变更记录**:2026-10-14:新增。
- **调用方**:`app/bootstrap.py:205`(启动日志只取 `split("?")[0]`,**不打印码**)、
  `app/cli.py:200`(JSON 字段 `url`)

## 三条不变量

### (a) 引导码只以文件为载体

引导码**永不**写进日志、数据库或邮件:

- 数据库里没有它的位置——`users.password_hash` 只有 Argon2id 哈希,
  `data/admin.hash` 同理;它是**独立于口令哈希**的第二条凭证;
- 日志只写**文件路径**(`app/bootstrap.py:199`–`:202` 用 `code_path(settings)`)与
  引导地址的**基地址部分**(`:203`–`:206` 用 `bootstrap_url(...).split("?")[0]`);
- 文件本身落盘权限 600(`ensure_code`),因为它是「读到即等价于拿到管理员」的凭证,
  权限松了就是同机其他用户的读权限问题;
- 唯一的「明文出口」是**管理员主动发起**的两处:`python -m app.cli bootstrap` 的 stdout,
  以及管理员自己打开 `/bootstrap?code=…` 时表单里的预填值(`templates/bootstrap.html:23`)。
  审计日志 `bootstrap/*` 只记客户端与账号名,不记码(`app/routers/auth.py:690`–`:723`)。
- 因此**不要**为了「方便」把引导码加进启动日志、审计 detail 或任何通知邮件。

### (b) 通道只在首位管理员 `must_change_password` 为真时存在

- `pending_owner()` 是**唯一**判据;`sync_code_state()` 是文件与状态之间唯一的同步点;
- 口令一设置(`POST /bootstrap` 成功,或 CLI `reset-admin-password` 定稿),
  `must_change_password` 立刻清零、文件立刻删除,`GET /bootstrap` 与 `POST /bootstrap`
  立刻 404(`app/routers/auth.py:652`、`:700`);
- 所以它**不是**常驻后门:首位管理员在正常使用中不会长期停留在这个状态,
  一旦改过口令,这个文件就不存在了;
- 反过来,若首位管理员的口令是**注入**的(`ADMIN_PASSWORD(_HASH)`),
  创建账号时 `must_change_password` 为假(`app/bootstrap.py:106`),
  这条通道从来不会开,CLI `bootstrap` 会输出 `status=not_pending`。

### (c) 与 `data/session_secret` 无关,但随 `DATA_DIR` 一起丢

- 引导码**不派生自**服务端密钥:`ensure_code()` 用 `secrets.token_urlsafe(24)`,
  与 `data/session_secret` 没有任何关系。因此轮换 `data/session_secret`
  (会让**所有账号固定订阅地址**与**所有验证码哈希**失效,见 `AGENTS.md` 不变量 23)
  **不会**影响引导码,反之亦然;
- 但 `code_path()` 落在 `settings.secret_dir`,而它等于 `data_dir`
  (`app/config.py:131`–`:132`),所以三者同处一室:`data/bootstrap_code`、
  `data/session_secret`、`data/admin.hash`。**删掉 `DATA_DIR` 会连同引导码一起丢**,
  恢复备份时也要把 `bootstrap_code` 一起恢复(否则只能靠
  `python -m app.cli reset-admin-password` 重新设一个口令);
- 从「已改过口令」的备份恢复时,`sync_code_state()` 会在启动时删掉那个备份里的码文件
  (见 `ensure_code` / `clear_code` 条目的不变量),不要指望恢复引导码来重开通道。

## 已知边界(归档时核对源码得到的现状)

- **非 ASCII 引导码会抛 `TypeError`**:`verify_code()` 在两侧都非空时直接调
  `hmac.compare_digest(expected, text)`,而它对含非 ASCII 字符的 `str` 会
  `raise TypeError("comparing strings with non-ASCII characters is not supported")`。
  管理员在 `/bootstrap` 表单里粘贴中文时,`app/routers/auth.py:704` 不会被
  `except` 兜住,表现为 500 而不是 401。**当前没有测试覆盖这条分支**
  (`tests/test_bootstrap_access.py` 只试了错码、空串、`None`)。
- **文件写入不是原子替换**:`ensure_code()` 直接 `write_text` + `chmod`;
  引导码是一次性救援凭证,没有订阅产物那套「先临时文件再 `os.replace`」的要求。
- **Windows 上 600 权限不生效**:`path.chmod(0o600)` 在 Windows 上只切换只读位、
  不构成访问控制(POSIX 上的异常也被 `contextlib.suppress(OSError)` 吞掉),
  权限保护只在 POSIX 上成立;真机(容器)是 Linux,按 600 生效。
- **`reset-admin-password` 之后的码状态由 CLI 自己同步**
  (`app/cli.py:236`–`:239`),并不经过 `sync_code_state()`——两条路径的结果一致,
  但逻辑不在同一个函数里。

## 变更记录

- 2026-10-14:新增本文档。归档首位管理员引导码的全部常量
  (`BOOTSTRAP_FILENAME` / `BOOTSTRAP_CODE_BYTES` / `BOOTSTRAP_CODE_PREFIX` /
  `BOOTSTRAP_QUERY`)与全部对外函数(`code_path` / `read_code` / `ensure_code` /
  `clear_code` / `verify_code` / `pending_owner` / `sync_code_state` / `bootstrap_url`),
  并写明三条不变量:文件是唯一载体、通道只在 `must_change_password` 为真时存在、
  与 `session_secret` 无关但随 `DATA_DIR` 一起丢。