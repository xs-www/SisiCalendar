# `templates/`

Jinja2 模板。本次涉及设置页、日程表单与邮件队列片段。约定的上下文键来自
`app/routers/pages.py`(见 `docs/static/app-routers-pages.md`)。

## `templates/partials/settings_form.html`

- **变更**:「默认时区」由 `<input list="tz-list-settings">` 改为按地区分组的 `<select name="default_timezone">`
  - 选项文本:`{{ item.index }}. {{ item.label }}({{ item.iana }})`,如 `1. 北京时间(Asia/Shanghai)`
  - `value` 仍是 IANA 名,保证已有表单/接口调用不变
  - 当 `current_timezone` 不在 `timezone_known` 中时,补一个
    `{{ current_timezone }}(当前值,不在常用清单中)` 且 `selected`,不会静默改掉用户配置
  - 下拉框下方 `<p class="hint">` 说明序号可被 `event` 接口直接使用
- **移除**:`<datalist id="tz-list-settings">`(不再有输入框引用它)
- **上下文**:`timezone_groups`、`timezone_known`、`current_timezone`、`overrides`、`settings`
- **测试**:`test_settings_page_renders_timezone_dropdown`、`test_settings_page_keeps_custom_timezone`

## `templates/event_form.html`

- **变更**:
  - 时区输入框的标签由「时区 IANA」改为「时区 IANA 名或序号」
  - 输入框下方 `<p class="hint">` 给出序号与中文名示例(`1` = 北京时间、`11` = 悉尼时间)
  - `<datalist id="tz-list">` 先列常用清单(`<option value="Asia/Shanghai" label="1. 北京时间">`),
    再列全部 IANA 名
- **上下文**:`timezone_choices`、`timezones`
- **测试**:`test_event_form_mentions_timezone_index`

## `templates/partials/sync_status.html`

- **变更**:邮件队列表格新增两列
  - **操作时间**:`delivery.created_at`,即任务创建时刻,`title` 给出到秒的时间与时区
  - **发送时间**:`delivery.sent_at`,未发送显示 `—`
  - 「下次重试」由 `strftime` 改为同一套 `localtime` 过滤器,三个时间列显示口径一致
  - 表格上方 `<p class="hint">` 注明「操作时间为任务创建时刻;时间均按日历默认时区 X 显示」
- **列顺序**:版本 / 收件人 / 类型 / 状态 / 尝试 / 操作时间 / 发送时间 / 下次重试 / 错误 / 操作
- **上下文**:`deliveries`、`default_timezone`(新增)、`sync_state`、`status_labels`、`change_labels`、`csrf_token`
- **过滤器**:`localtime`(见 `docs/static/app-deps.md`)
- **测试**:`test_mail_queue_shows_operation_and_sent_time`

## `templates/partials/tokens.html`(新增)

- **用途**:设置页的「远程接入令牌」卡片内容,HTMX 局部刷新,目标 `#agent-tokens`
- **结构**:
  - `new_token` 非空时显示一次性明文:只读 `<input class="mono token-reveal" readonly>`,
    提示「只显示这一次」——**不使用内联脚本**(严格 CSP 禁止 `onclick`),需手动选中复制
  - 新增表单:`label` + `acknowledge` 复选框(确认该令牌可远程建日程并触发邮件),
    提交到 `POST /admin/partials/tokens`
  - 列表:名称(可改名)/ 指纹 / 状态 / 创建时间 / 操作(停用·启用、撤销)
- **指纹列**:展示 `token_hash[:16]`,与审计日志 `agent:<指纹>` 对得上
- **上下文**:`tokens`、`csrf_token`、`new_token`、`notice`、`error`
- **测试**:`tests/test_agent_tokens.py` 全文件

## `templates/settings.html`

- **变更**:在「同步人员」卡片之后新增「远程接入令牌」卡片,`{% include "partials/tokens.html" %}`;
  卡片头显示 `已配置 N 枚` 或 `仅 .env 兜底`,并在提示里说明明文只显示一次、
  库内只存哈希、`AGENT_API_TOKEN` 仍作为兜底生效
- **上下文**:新增 `tokens`(见 `docs/static/app-routers-pages.md`)
- **测试**:`tests/test_agent_tokens.py::test_settings_page_carries_the_token_card`

## 变更记录

- 2026-09-29:设置页改下拉框;日程表单补序号提示与候选标签;邮件队列新增操作/发送时间。
- 2026-09-29:新增 `partials/tokens.html`;设置页新增远程接入令牌卡片。
