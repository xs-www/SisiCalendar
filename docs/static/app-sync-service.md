# `app/sync_service.py`

同步编排:把「保存 → 生成 ICS → 固化版本 → 创建发信任务」串成一条链路。
本次变更集中在**邮件正文那一行时间怎么写**。

## 函数

### `offset_label(offset) -> str`

- **位置**:`app/sync_service.py:50`
- **用途**:把固定 UTC 偏移量渲染成标签:`UTC+11:00` / `UTC-05:30` / `UTC`(零偏移)
- **参数**:`timedelta`;秒级偏移按分钟向下取整(现实中不会出现)
- **副作用**:无
- **测试**:`tests/test_mail.py::test_event_time_text_uses_submitted_offset`、
  `::test_utc_offset_is_not_treated_as_missing`

### `event_time_text(event, *, utc_offset=None) -> str`

- **位置**:`app/sync_service.py:60`
- **用途**:邮件正文里「事件时间:」那一行的文本
- **行为**:
  - 默认(`utc_offset=None`)按**事件的参照时区** `event.timezone` 表述,标签写 IANA 名;
  - 给出 `utc_offset` 时改按该固定偏移表述,标签写 `UTC±HH:MM`。
- **为什么需要第二种**:`start_at` 带偏移量说明**提交者当时就在那个时区**,
  它写下的钟点才是提交者眼里的 14:00;而事件的参照时区可能是常驻时区(默认时区或
  `timezone` 字段),两者不一定是同一个。邮件是纯文本、收件端不做换算,所以只能按提交时区渲染。
- **不变量**:仅影响**正文那一行文本**。事件的 `timezone`、落库的 naive UTC、
  ICS/订阅源、附件内容都不受影响;全天事件与「时间未设置」两个分支不变
- **测试**:`tests/test_mail.py::test_event_time_text_uses_submitted_offset`、
  `::test_utc_offset_is_not_treated_as_missing`

### `input_time_text(event, *submitted) -> str`

- **位置**:`app/sync_service.py:99`
- **用途**:从调用方提交的原始时间文本里挑出偏移量,再交给 `event_time_text`
- **参数**:依次传 `payload.start_at`、`payload.end_at`,取第一个带偏移量的
- **实现要点**:不能用 `a or b`——UTC 的偏移量是 `timedelta(0)`,布尔值为假,
  会把「提交的是 UTC」误判成「没提交偏移量」
- **返回**:两类情况:提交文本带偏移量 → 按提交钟点;都不带(网页表单的 `datetime-local`、
  纯日期、只给 `duration_minutes`)→ 按事件参照时区
- **测试**:`tests/test_mail.py::test_agent_submitted_offset_reaches_the_mail_body`
  (走完整 Agent HTTP 链路,断言发信任务记录里的 `event_time_text`)

### `publish(session, *, settings=None, change_type=..., event=None, event_title=None, time_text=None, force_new_version=False) -> SyncOutcome`

- **位置**:`app/sync_service.py:126`
- **签名未变**;`time_text` 这个既有参数被真正用起来了:
  `app/routers/api_agent.py` 与 `app/routers/api_events.py`(新建/修改)现在都传
  `time_text=input_time_text(event, payload.start_at, payload.end_at)`
- **未传时的行为**:保持原样,按事件参照时区渲染(网页表单、手动同步、后台修复都走这条)
- **家庭版(2026-10-08)的编排变化**——`publish` 自身不加参数,变化全在它调用的两步里:
  1. `export_calendar(...)` 现在一次产出并集文件 + **每个订阅目标的发布文件与快照**
     (见 `docs/static/app-exporter.md`);返回的 `result.version` 与 `result.changed`
     语义不变,`result.targets` 列出本次的目标键
  2. 只有在 `result.changed` 为真时才 `ensure_delivery_tasks(...)`,而它现在
     **按收件人范围逐目标**选附件:账号收件人拿「他可见日历里尚未结束的日程」,
     外部邮箱拿「家庭共享日历里尚未结束的日程」(见 `docs/static/app-mailqueue.md`)
- **不变量**:
  - 调用前调用方必须已经提交过一次事务(事件已落库、`dirty` 已置位),
    这样即便导出失败,用户的编辑也不会丢失
  - **版本号仍由全库并集决定**:任何一个成员改动自己的私人日历,都会推进全局
    `export_version`,并让所有目标重新落盘;各目标内容不同但版本号相同
  - `changed=False` 时不建任务:同一版本对同一收件人的任务早已存在(幂等键不变)
  - 邮件或文件问题**不得**影响日程保存:`ExportError` 与其它异常都走
    `_persist_failure` + 回滚,返回 `SyncOutcome(ok=False, ...)`
- **测试**:`tests/test_family_mail.py::test_account_and_external_recipients_coexist_and_are_idempotent`
  (直接调 `publish` 后用同版本再调 `ensure_delivery_tasks` 断言返回 0)、
  `::test_external_recipient_only_gets_family_calendar`、
  `::test_feed_files_exist_for_every_target_after_export`

## 影响面

- 邮件正文的时间口径与网页/日历**可以不同**,这是有意为之:邮件说「你输入时的钟点」,
  日历与 ICS 说「你所在地的时刻」。
- 同一事件在**修改**时的偏移量同样生效(`PATCH /api/v1/events`);
  删除通知不带时间行,不受影响。
- 家庭版之后 `publish` 的调用方没变(`api_agent` / `api_events` / `regenerate` /
  `repair_if_needed` / 设置页动作),但它们触发的副作用面变大了:
  一次保存会重写**全部目标**的订阅文件与快照,并按收件人范围排队多封邮件。

## 变更记录

- 2026-10-06:新增 `offset_label`、`input_time_text`,`event_time_text` 增加 `utc_offset`
  参数;邮件正文改按调用方提交的偏移量表述,事件参照时区不变。
- 2026-10-08:家庭版多目标编排归档 —— `publish` 签名不变,但 `export_calendar` 产出全部
  订阅目标、`ensure_delivery_tasks` 按收件人范围取附件;版本号仍由并集决定。