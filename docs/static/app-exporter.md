# `app/exporter.py`

ICS 导出服务:生成、原子替换、版本状态、快照与一致性检查。
家庭版(2026-10)起,一次导出产出**一整套按目标切分的文件**。

## 核心区分:并集产物 vs 订阅目标 vs 邮件附件

| 产物 | 内容 | 依赖挂钟时间 | 参与版本哈希 | 谁在看 |
|---|---|---|---|---|
| `export/calendar.ics` | **全部**未删除事件(并集) | 否 | 是(`state.content_hash`) | 内部一致性检查与运维,不对外暴露 |
| `export/feeds/user-<账号id>.ics` | 该账号可见范围(个人 + 共享) | 否 | 否 | 该账号的 `/calendar/<令牌>.ics` |
| `export/feeds/family.ics` | 家庭共享日历范围 | 否 | 否 | 家庭令牌 `/calendar/<令牌>.ics` |
| `snapshots/calendar-v{N}.ics` | 同并集,逐字节相同 | 否 | 是,哈希即 `CalendarExport.content_hash` | 邮件回退 |
| `snapshots/calendar-v{N}-<目标键>.ics` | 该目标的完整版 | 否 | 否 | 邮件任务重试 |
| `snapshots/calendar-v{N}-<目标键>-upcoming.ics` | 该目标里**尚未结束**的日程 | 是(按此刻) | **否** | 邮件附件 |

这是不变量 3 的边界所在:导出内容必须只取决于数据库状态,所以并集文件与各目标的
**完整**快照绝不过滤;「未来」是相对挂钟时间的概念,只允许出现在不参与哈希的邮件附件上。
邮件任务仍然只引用已落盘的不可变文件,因此不变量 6(附件取自该任务的不可变快照、
重试不冒充新版)保持不变。

## 多目标导出(2026-10-08 新增)

### `FAMILY_TARGET_KEY` / `FeedTarget`

- **位置**:`app/exporter.py:58` / `app/exporter.py:66`
- **常量**:`FAMILY_TARGET_KEY = "family"`,家庭共享范围的关键字
- **结构**:`FeedTarget(key, user_id, name, calendar_ids)`(frozen dataclass)
  - `key`:目标键,账号目标是 `user-<id>`,家庭目标是 `family`
  - `user_id`:**家庭目标是 `None`**——这是区分两类目标与两类订阅地址的唯一标志
  - `calendar_ids`:该目标导出时使用的日历范围(元组,顺序稳定)

### `feed_target_key(*, user_id) -> str`

- **位置**:`app/exporter.py:86`
- **用途**:由账号 id 推导目标键
- **返回**:`user_id` 为空 → `"family"`,否则 `"user-<user_id>"`
- **不变量**:文件名、快照名与邮件任务的目标键都**必须**经此函数产生,不要各自拼字符串;
  `app/mailqueue.py::ensure_delivery_tasks` 与 `tests/test_family_mail.py` 都依赖它的一致命名

### `feed_path_for(settings, *, user_id) -> Path`

- **位置**:`app/exporter.py:90`
- **用途**:订阅目标的**发布文件**,`export/feeds/<目标键>.ics`
- **不变量**:这是**唯一**对外暴露的订阅产物;`app/routers/feed.py` 用它推导要返回的文件,
  不得直接读 `export/calendar.ics`
- **测试**:`tests/test_family_mail.py::test_feed_files_exist_for_every_target_after_export`

### `target_snapshot_path_for(settings, version, target_key) -> Path`

- **位置**:`app/exporter.py:95`
- **用途**:某个目标的**完整**版本快照,`snapshots/calendar-v{版本:06d}-<目标键>.ics`
- **不变量**:同目录、同版本号前缀,是 `cleanup_snapshots` 的 glob 能一次清干净的前提

### `resolve_targets(session) -> list[FeedTarget]`

- **位置**:`app/exporter.py:100`
- **用途**:列出本次导出要产出的全部订阅目标:**每个启用账号一个 + 家庭共享一个**
- **过滤规则**:
  - `list_users(session)` 里 `user.enabled` 为假的账号**直接跳过**——停用账号从导出与
    邮件目标里彻底消失(不变量 15)
  - 账号目标的 `calendar_ids = target_calendar_ids(session, user_id=...)`,即该账号
    个人日历 + 他能看到的共享日历
  - 家庭目标固定最后追加,`calendar_ids = family_calendar_ids(session)`,**只含共享日历**
- **副作用**:无(纯查询)
- **测试**:`tests/test_family_mail.py::test_resolve_targets_covers_each_enabled_user_and_family`、
  `::test_disabled_user_is_dropped_from_targets`

### `build_target_data(events, target, *, settings) -> bytes`

- **位置**:`app/exporter.py:232`
- **用途**:把给定事件集合渲染成某个目标的 ICS 内容,只取决于传入的事件与目标名称
- **日历名**:`"{settings.calendar_name} · {target.name}"`,目标名为空时只用基础名
  ——订阅端在日历应用里能直接分辨这是谁的日历
- **副作用**:无(纯函数)

### `active_events(session, *, calendar_ids=None) -> list[Event]`

- **位置**:`app/exporter.py:153`
- **本次变更**:新增 `calendar_ids` 范围过滤
- **参数语义**:
  - `None`(缺省)= **不过滤**,全库并集路径使用
  - 非空列表 = 只取这些日历的事件
  - **空列表 = 直接返回 `[]`**,绝不退化成「不过滤」
- **不变量**:排序键(`all_day.desc()`, `start_at_utc.asc()`, `start_date.asc()`,
  `created_at.asc()`, `id.asc()`)保持不变,同一数据必须生成同一哈希

### `upcoming_events(session, *, settings, horizon_days=..., calendar_ids=None) -> list[Event]`

- **位置**:`app/exporter.py:202`
- **用途**:挑出某个范围内「**尚未结束**」的事件,顺序与 `active_events` 一致
- **判据**:复用查询侧的展开逻辑——`build_range(None, None, default_days=horizon)` 得到窗口,
  再把 `start_utc` 替换成**此刻**(`utcnow()`),然后对每个事件调用 `expand_occurrences`。
  三种情况都不会误删:区间与未来重合的一次性事件、**正在进行**的事件(起点在过去但尚未结束)、
  **起点很早但仍在重复**的事件
- **分界是此刻,不是「今天 00:00」**:当天早些时候结束的事件同样被排除(「今天上午开完的会」
  不会跟着下一次邮件发出去)。全天事件没有时刻可言,仍按应用默认时区的当地日期比较,
  所以「今天」的全天事件照旧保留——实现上只替换 `start_utc`,不动 `start_date`
- **确定性**:附件是「每个版本只写一次、之后永不重写」的产物(重试复用同一文件),
  所以分界随钟表移动不会造成同一版本前后不一致;它本来也不参与 `content_hash`
- **参数**:`horizon_days` 默认 5 年(`MAIL_ATTACHMENT_HORIZON_DAYS = 366 * 5`,位置 `:55`),
  用来给重复规则留出展开空间;`rrulestr.between()` 只枚举窗口内实例,老事件不会拖慢速度。
  `calendar_ids` 限定范围(账号收件人只看得到自己的日历),`None` 表示全部
- **副作用**:无(纯查询)
- **测试**:`tests/test_mail.py::test_attachment_drops_past_events_but_feed_keeps_them`、
  `::test_attachment_keeps_ongoing_events`、`::test_attachment_drops_events_that_ended_today`、
  `::test_attachment_keeps_all_day_events_of_today`、
  `::test_attachment_keeps_recurring_series_started_in_the_past`、
  `::test_attachment_drops_recurring_series_that_already_ended`;
  范围切分:`tests/test_family_mail.py::test_account_recipient_gets_own_visible_scope`、
  `::test_external_recipient_only_gets_family_calendar`

### `mail_snapshot_path_for(settings, version, *, target_key=None) -> Path`

- **位置**:`app/exporter.py:189`
- **用途**:邮件专用快照的文件名
- **本次变更**:新增 `target_key` 参数
  - 给出 `target_key` → `snapshots/calendar-v{版本:06d}-<目标键>-upcoming.ics`
  - **不给**(默认 `None`)→ `snapshots/calendar-v{版本:06d}-upcoming.ics`,
    保留旧版并集命名,供升级前创建的历史版本继续使用
- **不变量**:与目标的完整快照**同目录同版本号**;发信侧(`app/mailqueue.py::attachment_source`)
  用同一函数推导路径,不得各自拼字符串
- **测试**:`tests/test_mail.py::test_attachment_hash_matches_task_snapshot`、
  `tests/test_family_mail.py::test_each_recipient_attachment_comes_from_its_own_snapshot`

### `_build_mail_attachment(session, settings, target, *, fallback) -> bytes`

- **位置**:`app/exporter.py:249`
- **用途**:生成某个目标的邮件附件内容;失败时**回退为该目标的完整日历**(`fallback`)
- **本次变更**:多了 `target` 参数,取数走 `upcoming_events(..., calendar_ids=target.calendar_ids)`
- **不变量**:附件是次要产物,它的生成失败**不得**让发布或订阅失败,所以这里吞掉异常并记日志
- **测试**:无独立用例(异常分支靠回退语义覆盖)

### `_missing_target_files(settings, targets) -> bool`

- **位置**:`app/exporter.py:267`
- **用途**:任一目标的发布文件缺失就返回真——新账号/新令牌刚建立时它们还不存在
- **调用方**:`export_calendar`(早退分支要多判这一条)、`sync_if_dirty`、`verify_consistency`
- **说明**:这是「不 dirty 但订阅端仍会 503」的补丁口;少了它,新建的账号订阅地址
  在第一次导出前永远拿不到文件

### `ExportResult`

- **位置**:`app/exporter.py:76`
- **本次变更**:新增字段 `targets: tuple[str, ...] = ()`,记录本次导出的目标键集合
  ——调用方(日志、运维脚本)不必再自己猜有哪些目标

## 函数

### `export_calendar(session, *, settings=None, force_new_version=False) -> ExportResult`

- **位置**:`app/exporter.py:273`
- **本次变更**:
  - 先 `targets = resolve_targets(session)`,早退分支额外要求 `not _missing_target_files(...)`
  - 分配新版本时,在同一段 `try` 内按目标循环落盘三份文件:
    **目标发布文件(`feed_path_for`)→ 目标完整快照(`target_snapshot_path_for`)→
    目标邮件快照(`mail_snapshot_path_for(target_key=...)`)**;
    并集的版本快照与 `export/calendar.ics` 仍在循环之前写
  - 返回值带上 `targets`
- **不变量**:
  - **版本号与 `content_hash` 仍只由并集**算出:任何成员的改动都会推进全局
    `export_version`,各目标文件不参与这个判断
  - `changed=False` 的早退分支不写任何文件:该版本的全部快照在它被创建时就已存在
  - 任一次原子写失败仍按原逻辑标记 `dirty` 并抛 `ExportError`;此时**并集文件可能已经是
    新版、部分目标的文件还是旧版或缺失**,所以恢复靠 `error` 分支置位 `dirty` 后的下一次
    重导 + `_missing_target_files` 兜底,而不是回滚文件
- **测试**:`tests/test_family_mail.py::test_feed_files_exist_for_every_target_after_export`;
  全量导出相关用例(`tests/test_ics.py`、`tests/test_feed.py`)未变即证明并集哈希路径没动

### `sync_if_dirty(session, *, settings=None) -> ExportResult | None`

- **位置**:`app/exporter.py:390`
- **本次变更**:`needs_export` 增加 `_missing_target_files(resolved, targets)` 一条
- **用途**:订阅入口与后台任务使用:仅在需要时重新生成

### `verify_consistency(session, *, settings=None) -> tuple[bool, str]`

- **位置**:`app/exporter.py:407`
- **本次变更**:dirty 分支之后新增「订阅文件缺失」分支——任一目标发布文件不存在时重新导出,
  返回 `(True, "订阅文件缺失,已重新生成 version=...")`;重新生成失败返回 `(False, ...)`
- **说明**:这一段与「并集文件哈希对不上」的既有分支相互独立,顺序是 dirty → 目标文件缺失 → 哈希
- **测试**:`tests/test_feed.py::test_verify_consistency_regenerates_missing_file`

### `cleanup_snapshots(session, *, settings=None) -> int`

- **位置**:`app/exporter.py:456`
- **本次变更**:删除方式由「只删 `export.snapshot_path`」改成按 glob
  `calendar-v{版本:06d}*.ics` 清理,一次带走该版本的**并集快照、各目标完整快照与
  各目标邮件附件快照**
- **返回值**:仍按「被删文件数」累计;只有存在待清理文件时才 +1 的旧语义改为逐文件计数
- **不变量**:当前发布版本永不清理;有未终结邮件任务的版本跳过
- **副作用**:删文件(不可逆);`OSError` 一律 suppress

## 变更记录

- 2026-10-06:新增邮件专用快照(`-upcoming.ics`,仅尚未结束的日程)。发布文件与版本快照的
  内容、哈希与版本判定全部不变;`cleanup_snapshots` 连带清理邮件快照。
- 2026-10-06(同日):分界由「今天 00:00」改为「此刻」——当天已结束的事件也不再进附件;
  全天事件仍按当地日期(只替换 `start_utc`,不动 `start_date`)。
- 2026-10-08:家庭版多目标导出 —— 新增 `FAMILY_TARGET_KEY`/`FeedTarget`/`feed_target_key`/
  `feed_path_for`/`target_snapshot_path_for`/`resolve_targets`/`build_target_data`/
  `_missing_target_files`;`active_events`/`upcoming_events` 支持 `calendar_ids`,
  `mail_snapshot_path_for` 支持 `target_key`;`ExportResult.targets` 记录目标集合;
  `export_calendar`/`sync_if_dirty`/`verify_consistency` 感知目标文件缺失;
  `cleanup_snapshots` 按版本 glob 清理全部快照。
  **版本号与 `content_hash` 仍只由全库并集决定**,`export/calendar.ics` 降级为内部产物。