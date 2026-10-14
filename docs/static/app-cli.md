# `app/cli.py`(初始口令救援子命令)

> **本文只归档 2026-10-14 新增的两个子命令**:`bootstrap` 与 `reset-admin-password`。
> 其余运维命令(`backup` / `backups` / `restore` / `export` / `verify` / `process-mail` /
> `hash-password` / `credential`)见《部署与运维》。

两个子命令的共同点:都先跑 `_prepare(settings)`(`app/cli.py:44` →
`app/bootstrap.py::bootstrap_database`,即**迁移 + 建引擎**),都在
`session_scope()` 里操作数据库,都**不需要口令、不需要看 `docker logs`**,
`stdout` 一律是**一行 JSON**(`json.dumps(..., ensure_ascii=False)`,
`main()` 的返回值就是进程退出码,见 `app/cli.py:330`–`:336`)。
凭据来源、`ADMIN_USERNAME` / `ADMIN_PASSWORD(_HASH)` 的语义见
[`app-bootstrap.md`](app-bootstrap.md);引导码本身见
[`app-bootstrap-access.md`](app-bootstrap-access.md)。

## argparse 入口

| 子命令 | 位置 | 参数 |
|---|---|---|
| `bootstrap` | `app/cli.py:281`–`:287` | `--rotate`(`store_true`,重新生成引导码,旧码立即失效) |
| `reset-admin-password` | `app/cli.py:289`–`:296` | `--password`(默认 `""`,不推荐:会留在命令历史中)、`--generate`(`store_true`,生成一次性随机口令) |

`main(argv)`(`app/cli.py:330`)先 `_configure_logging()`,再 `build_parser().parse_args(argv)`,
然后 `get_settings()` → `_warn_if_container_path(settings)`(`:302`,提醒 `.env` 里的
`DATA_DIR=/data` 在 Windows 宿主机上会解析成 `\data`,即**另一个库**)→
`settings.ensure_directories()` → `args.func(args, settings)`。

## `cmd_bootstrap(args, settings) -> int`

- **位置**:`app/cli.py:165`;argparse 入口 `app/cli.py:281`–`:287`;`set_defaults(func=cmd_bootstrap)`
- **用途**:查看(必要时生成)首位管理员的引导码与**完整引导地址**;
  这是「口令随机生成、日志又读不到」时的第一条救援路径
- **参数**:`args.rotate`(`--rotate`);其余参数不用
- **行为与返回**:
  1. `_prepare(settings)`(`:171`)——跑迁移、建引擎;
  2. `session_scope()` 内取 `app/bootstrap_access.py::pending_owner(session)`(`:175`);
  3. **通道已关闭**(`owner is None`,`:176`–`:189`):`stdout` 打印
     `{"status": "not_pending", "hint": …}`,其中 `hint` 为
     「首位管理员的口令不是初始口令,引导通道已关闭。忘记口令请用
     `python -m app.cli reset-admin-password`。」(`app/cli.py:181`–`:184`),
     **仍返回 0**(不是错误,而是「这条路不需要了」);
  4. **通道开着**(`:190`–`:205`):`ensure_code(settings, rotate=bool(args.rotate))` →
     退出会话后 `stdout` 打印
     `{"status": "pending", "username": …, "code_file": …, "code": …, "url": …, "note": "口令设置成功后引导码文件会自动删除,该地址随即失效。"}`,
     返回 0。
- **stdout 的 JSON 字段**:`status`(`"pending"` / `"not_pending"`)、
  `username`(首位管理员账号名)、`code_file`(`<DATA_DIR>/bootstrap_code` 的绝对路径)、
  `code`(引导码明文,`sisi_boot_…`)、`url`(`app/bootstrap_access.py::bootstrap_url()` 拼的
  完整引导地址)、`note`(地址失效条件的提醒);通道关闭时只有 `status` + `hint`。
- **副作用**:可能创建引导码文件(`ensure_code`,权限 600);`--rotate` 时**覆盖**它,
  旧码立即失效;`_prepare` 会顺带跑一次 Alembic 迁移。**不写库**。
- **退出码**:`0`(两种状态都是 0——`status=not_pending` 不是失败);
  参数非法由 argparse 自己返回 `2`(见下文的通用约定)。
- **不变量**:
  - **引导码只出现在 stdout 这一处**,不得写进日志(与 `app/bootstrap.py:199`–`:206`
    同一条约定:日志只记文件路径与基地址);
  - 不带 `--rotate` 时**不得换码**(`ensure_code` 先读后写),
    否则管理员刚抄下来的码当场作废;
  - 输出必须是**单行 JSON**:调用方与测试按 `stdout.strip().splitlines()[-1]` 取最后一行;
  - 「通道已关闭」必须给出下一步(`hint` 指向 `reset-admin-password`),并且**返回 0**
    而不是报错——它是最常见的正常结果。
- **测试**:`tests/test_bootstrap_access.py::test_cli_bootstrap_reports_not_pending`
  (断言 `status == "not_pending"` 且 `hint` 里含 `reset-admin-password`)、
  `::test_cli_bootstrap_prints_the_code_and_url`(`status == "pending"`、
  `code` 等于文件内容、`url` 以 `/bootstrap?code=<code>` 结尾、
  `--rotate` 后码变了)

## `cmd_reset_admin_password(args, settings) -> int`

- **位置**:`app/cli.py:209`;argparse 入口 `app/cli.py:289`–`:296`;
  `set_defaults(func=cmd_reset_admin_password)`
- **用途**:**口令彻底丢失**(连初始口令都没有、引导码文件也被删)时的最后一条救援路径:
  直接给首位管理员设一个新口令,不依赖任何旧凭证
- **参数**:`args.generate`(`--generate`)、`args.password`(`--password`,默认 `""`)
- **行为**:
  1. `_prepare(settings)`(`:215`);
  2. `session_scope()` 内取 `app/accounts.py::owner_user(session)`(`:220`);
     **库里没有任何账号** → `stderr` 打印「数据库里没有任何账号」并返回 **1**
     (`:221`–`:223`,源码标了 `# pragma: no cover - 引导保证存在`,实际不可达);
  3. 决定口令与新状态位:
     - `--generate` → `password = secrets.token_urlsafe(12)`、`must_change = True`
       (`:224`–`:226`);
     - 否则 → `password = args.password or getpass.getpass("请输入新的管理员口令:")`、
       `must_change = False`(`:228`–`:229`)。**注意 `--password ""` 等于没给**,
       会转成交互式输入(所以「空口令」不会被静默设置成功);
  4. `app/accounts.py::set_password(session, owner, password, must_change=must_change)`
     (`:231`);抛 `AccountError`(口令不合规等)→ `stderr` 打印各字段错误
     (以 `;` 拼接,复用 ORM 层文案)→ 返回 **2**;
  5. 与新的状态位对齐(`:235`–`:239`):仍待改密 → `ensure_code(settings)`(**保住引导码**,
     口令还没定稿);已定稿 → `clear_code(settings)`(**删掉引导码文件**,通道关闭);
  6. 退出会话后 `stdout` 打印 JSON,返回 **0**(`:247`–`:253`)。
- **stdout 的 JSON 字段**:`username`(首位管理员账号名)、`must_change`(布尔:
  `--generate` 为 `True`,显式口令为 `False`);`--generate` 时额外多一个
  `generated_password`(一次性口令明文)。
- **stderr 提示**:`--generate` 时追加两行
  「上面的一次性口令只显示这一次;用它登录后必须立刻改成自己的口令。」(`:248`–`:252`);
  无账号时是「数据库里没有任何账号」;口令不合规则是 `AccountError` 的各字段文案。
- **副作用**:写 `users.password_hash`、`must_change_password` 并刷新 `updated_at`
  (`app/accounts.py:256`–`:262`);旧口令从此不能登录,
  但**已经签发的会话不会因此失效**(会话是签名 Cookie,只带 `user_id`,
  `app/security.py::read_session` 不比对口令哈希);
  可能**删除**(已定稿)或**创建/复用**(仍待改密)引导码文件;
  **不写审计日志**(CLI 不经 HTTP,没有 `AuditLog` 调用点)。
- **退出码**:`0` 成功 / `1` 库里没有任何账号(不可达分支) / `2` 口令不合规
  (`set_password` 抛 `AccountError`);参数非法(如未知选项)由 argparse 返回 `2`。
- **不变量**:
  - **`generated_password` 只出现在这一次 stdout**,不得写日志、不得回显第二次
    (与「口令明文不进日志」同一条约定);
  - `--generate` **必须**把 `must_change_password` 置真:一次性口令只是过渡,
    登录后会被 `/login` 送到 `/admin/me` 强制改密(`app/routers/auth.py` 的
    `must_change_password` 分支);
  - 显式 `--password` 时 `must_change_password` 为假,且必须**同时清掉引导码文件**,
    否则通道会继续开着;
  - 重置口令**不改变**谁是首位管理员(只改哈希与状态位),也不动其他账号;
  - 这是**救援**命令,不校验旧口令、不要求登录;真正的防线是「能进服务器执行命令」。
- **测试**:`tests/test_bootstrap_access.py::test_cli_reset_admin_password_generates_a_working_one_time_password`
  (`--generate` → `must_change is True`、`stderr` 含「只显示这一次」、
  用 `generated_password` 能登录且被送到 `/admin/me`)、
  `::test_cli_reset_admin_password_accepts_an_explicit_password`
  (`--password` 给一个自定口令 → `must_change is False`、能登录且直达 `/admin`)

## 本项目 CLI 的通用约定(与本轮两个子命令相关)

- **退出码口径**:`0` 成功、`1` 运行期失败(既有子命令里是「一致性检查未通过」:
  `restore` / `export` / `verify` 都是 `0 if ok else 1`,`app/cli.py:85`、`:95`、`:119`)、
  `2` 参数或输入不合法(argparse 自己返回 `2`,本轮 `AccountError` 也落在这里);新命令沿用。
- **stdout 只给机器读**:一行 JSON,中文不转义(`ensure_ascii=False`);
  给人看的提醒一律走 `stderr`,所以 `... | jq` 之类的用法不会被提示语污染。
- **`_warn_if_container_path()` 先于一切**:在 Windows 宿主机上直接跑 CLI 时,
  `.env` 里的 `DATA_DIR=/data` 会解析成当前盘符根目录(`D:\data`),
  命令会悄悄操作**另一个**库(`app/cli.py:302`–`:327`);排查「引导码怎么没生成」时
  先看这条 warning。

## 没有测试覆盖的点

- `cmd_bootstrap` 的 `--rotate` 分支本身有用例覆盖,但**「通道已关闭 + `--rotate`」**
  没有单独用例(`pending_owner` 为 `None` 时根本不会走到 `ensure_code`,
  不会因为 `--rotate` 而凭空造出一个码文件)。
- `cmd_reset_admin_password` 的**交互式 `getpass` 分支**(不给 `--password`、不给
  `--generate`)、**无账号返回 1** 的分支,以及**口令不合规返回 2** 的分支
  都没有自动化用例(前者需要伪终端,后两者在源码里被标为不可达/需构造非法输入)。
- CLI 侧的 stdout 断言都从**最后一行**取 JSON,`hint` / `note` 的完整文案没有被逐字断言。

## 变更记录

- 2026-10-14:新增本文档。归档首位管理员口令救援的两个子命令:`bootstrap`
  (查看/轮换引导码与引导地址,`status=pending|not_pending` 都返回 0)与
  `reset-admin-password`(`--password` 或 `--generate`,退出码 0/1/2,
  `generated_password` 只在 stdout 出现一次),并记录 argparse 入口、stdout JSON 字段、
  stderr 提示与本项目 CLI 的退出码口径。