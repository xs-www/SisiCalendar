# `app/bootstrap.py`(家庭版引导)

本模块负责启动引导:跑 Alembic 迁移、把库补齐成家庭版(首位管理员、私人日历、家庭共享日历、
成员关系、历史数据归属),再做一致性检查与中断任务恢复。
本文只归档与家庭版改造相关的部分;`run_migrations` / `bootstrap_database` / `data_paths` 等既有函数未变。

结构变更只写在迁移里(家庭版建表见 `docs/static/migrations-0005.md`,收件人身份/范围拆分见
`docs/static/migrations-0006.md`),**数据回填只发生在这里**,原因是回填需要读环境里的管理员凭据
——迁移脚本拿不到 `Settings`。

## 新增

### `DEFAULT_FAMILY_CALENDAR_NAME`

- **位置**:`app/bootstrap.py:24`
- **取值**:`"家庭共享日历"`
- **用途**:升级时自动创建的那条共享日历的名称;管理员之后可以改名,也可以再建新的共享日历
- **不变量**:它只是**默认名**,不得用它来判断「哪条是家庭日历」——判据永远是
  `calendars.kind == 'shared'`(见 `app/accounts.py::list_shared_calendars`)
- **测试**:`tests/test_family_accounts.py::test_bootstrap_creates_first_admin_and_two_calendars`

### `ensure_family_setup(session, settings) -> None`

- **位置**:`app/bootstrap.py:48`;由 `startup_checks` 在 `app/bootstrap.py:182` 调用
- **用途**:幂等地把数据库补齐成家庭版,供每次启动执行
- **参数**:`session`(由 `app/db.py::session_scope` 提供,退出时统一提交);
  `settings` 只用于读管理员凭据(内部经 `app/security.py::ensure_admin_credential` /
  `load_admin_hash`),并复用 `snapshot`/`DATA_DIR` 之外的配置
- **执行顺序(每一步都在确有缺失时才动手)**:
  1. `app/bootstrap.py:86`–`:87`:取 `list_users`,记下 `first_upgrade = not users`。
  2. **只在没有任何账号时**(`app/bootstrap.py:89`–`:113`):调 `ensure_admin_credential(settings)`
     取得/生成首位管理员口令,再 `load_admin_hash` 读回摘要;读不到就抛
     `RuntimeError("缺少管理员凭据,无法初始化首位账号")`。随后创建首位管理员:
     账号名取 `normalize_username(settings.admin_username) or "admin"`,显示名「家庭管理员」,
     `role=admin`、`color=DEFAULT_USER_COLORS[0]`、`timezone=None`、`enabled=True`、
     **`email=None`**、`must_change_password=bool(本轮是否新生成口令)`,并立刻建出私人日历。
  3. `app/bootstrap.py:115`:取 `owner_user(session)` 作为**首位管理员**;仍为空则抛
     `RuntimeError("数据库里没有任何账号")`。后续所有历史数据都归到他名下。
  4. **没有共享日历时**(`app/bootstrap.py:119`–`:127`):建一条
     `DEFAULT_FAMILY_CALENDAR_NAME`,颜色取 `DEFAULT_USER_COLORS[1]`,首位管理员为 `owner`,
     其余现有成员为 `viewer`(见 `app/accounts.py::create_shared_calendar`)。
  5. `app/bootstrap.py:129`–`:132`:逐个账号补齐——`personal_calendar`(缺则建)、
     `join_shared_calendars`(缺则补 `viewer` 成员关系)、`sync_user_recipient`(有邮箱则建/更新
     账号收件人,没邮箱则确保没有收件人记录)。首位管理员初始 `email=None`,
     所以首次升级通常**不会**产生他的账号收件人记录。
  6. `app/bootstrap.py:135`–`:145`:**历史事件回填**——把 `Event.calendar_id IS NULL` 的行全部挂到
     首位管理员的私人日历(`calendar_id = owner 私人日历 id`),`created_by_user_id` 为空时补成
     首位管理员;条数写 info 日志。
  7. **只在 `first_upgrade` 时**(`app/bootstrap.py:147`–`:173`):
     - 把历史**外部**收件人(`user_id IS NULL AND scope_user_id IS NULL`)的**收信范围**
       绑到首位管理员:只写 `recipient.scope_user_id = owner.id`(`app/bootstrap.py:160`)
       并刷新 `updated_at`,**`user_id` 保持为空**,以保持它们「收到全部日程」的原有行为
       ——升级后才新建的外部邮箱只收家庭共享日历;
     - 把历史 `AgentToken.user_id IS NULL` 的令牌补成首位管理员并刷新 `updated_at`
       (`app/bootstrap.py:168`–`:173`)。
  8. `app/bootstrap.py:175`–`:177`:只要 `changed` 为真,调
     `app/exporter.py::mark_dirty` 并 `session.commit()`,让订阅端与邮件目标跟上新的范围。
- **返回**:`None`(无返回值;失败抛 `RuntimeError`)
- **副作用**:写账号/日历/成员/收件人/事件/令牌行;`changed` 时标记导出 dirty 并提交事务;
  产生 `warning`(新建首位管理员)与 `info`(新建共享日历、回填条数)日志
- **不变量(幂等)**:
  - 第二次运行的 `first_upgrade` 为 `False`,因此**不会**再改绑历史收件人与历史令牌,
    也不会重复建账号、私人日历或共享日历;
  - 已有成员关系与已有日历颜色、排序一律不动;
  - 已经在某条日历里的事件不会被再次搬运;`calendar_id` 非空的事件不受影响;
  - `must_change_password` 只在创建首位管理员时按「本轮是否新生成口令」决定;
  - **首次升级只写 `scope_user_id`,绝不写 `user_id`**:`user_id` 是「账号收件人」的身份标记,
    每个账号只能有一行。旧版本把**每一个**历史外部收件人都写成
    `user_id = 首位管理员`,于是同一账号名下出现多行;第二次启动时
    `app/accounts.py::sync_user_recipient` 用 `scalar_one_or_none()` 取行,抛
    `MultipleResultsFound`,应用启动失败、容器重启循环(真机 Docker 升级实际发生)。
    这正是 `0006` 迁移与本次引导改动要修掉的缺陷(见 `docs/static/migrations-0006.md`,
    `AGENTS.md` 不变量 21);
  - 被旧引导写坏的库(`user_id` 重复)不需要删除任何行:`0006` 迁移会把多余行降级,
    `sync_user_recipient` 也会容忍重复(告警 + 按 `users.email` 匹配 + 认不出就不动);
  - **已知的例外**:第 5 步对已有账号收件人会刷新 `sync_recipients.updated_at`
    (收件范围与邮件幂等键 `(export_version, recipient_id)` 不受影响)。所以「无副作用」是
    指不产生新的业务归属变更,而不是行级完全零写入。
- **测试**:
  - `tests/test_family_accounts.py::test_bootstrap_creates_first_admin_and_two_calendars`
    (首位管理员 `admin`/`admin` 角色、私人日历名「家庭管理员的日历」、共享日历
    `owner_user_id is None`、管理员在共享日历里是 `owner`)
  - `::test_bootstrap_is_idempotent`(二次运行不新增共享日历、首位管理员不变)
  - `::test_legacy_events_are_moved_to_owner_personal_calendar`
  - `::test_legacy_recipients_keep_owner_scope_on_first_upgrade`(本轮改为断言
    `scope_user_id == owner.id` 且 `user_id is None`)
  - `::test_new_external_recipient_stays_family_scoped`(升级后新建的外部收件人保持家庭范围)
  - `::test_legacy_agent_token_is_bound_to_owner`
  - `::test_owner_is_first_admin`
  - `tests/test_family_upgrade_recipients.py::test_two_legacy_recipients_survive_repeated_bootstrap`
    (两个历史收件人、连跑三次引导都不崩,`user_id` 仍为空、范围仍是首位管理员)
  - `tests/test_family_upgrade_recipients.py::test_bootstrap_recovers_from_duplicate_account_recipients`
    (被旧引导写坏的多行绑定库也能启动)

## 变更

### `startup_checks(session, settings) -> None`

- **位置**:`app/bootstrap.py:180`
- **本次变更**(2026-10):在一致性检查之前先调用 `ensure_family_setup(session, settings)`
  (`app/bootstrap.py:182`),文档串从「启动时的数据库与 ICS 一致性检查」扩写为
  「账号/日历初始化 + 一致性检查 + 中断任务恢复」
- **用途**:启动时先补齐家庭版数据,再 `verify_consistency` 校验导出状态、恢复中断的发信任务
- **返回**:`None`
- **副作用**:委托给 `ensure_family_setup`(写库)、`verify_consistency`(必要时标记 dirty)
  与 `recover_stuck_deliveries`(重置卡住的发信任务)
- **不变量**:引导必须**先于**一致性检查执行,否则 `calendar_id IS NULL` 的事件会被当作状态不一致;
  引导抛错时启动失败(宁可起不来,也不要带着半升级的库对外服务)
- **测试**:经 `tests/test_family_web.py` / `tests/test_family_pages.py` 的 `TestClient` lifespan
  间接覆盖(这些用例启动应用时会真的跑一次引导);引导本身的契约由
  `tests/test_family_accounts.py::test_bootstrap_*` 与
  `tests/test_family_upgrade_recipients.py` 直接覆盖

## 变更记录

- 2026-10-08:新增本文档。归档 `DEFAULT_FAMILY_CALENDAR_NAME`、
  `ensure_family_setup` 的幂等契约(首位管理员/私人日历/共享日历/成员关系/历史事件回填/
  首次升级的历史收件人与令牌归属)以及 `startup_checks` 新增的引导步骤。
- 2026-10-11:首次升级的历史收件人改为**只写 `scope_user_id`、`user_id` 保持为空**,
  并写明这正是修复「同一账号名下多行 → 第二次启动 `MultipleResultsFound` → 容器重启循环」
  的关键;补上 `tests/test_family_upgrade_recipients.py` 的两个回归用例;
  顺带订正全文的源码行号(部分旧行号在本轮之前就已过期)。