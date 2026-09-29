# `app/deps.py`

FastAPI 依赖与 Jinja2 模板环境。涉及模板过滤器 `localtime` 与 Agent 令牌鉴权。

## 变更

### `require_agent_token(request, session=Depends(get_db)) -> AgentPrincipal`

- **位置**:`app/deps.py:169`
- **用途**:校验 `Authorization: Bearer <token>`
- **本次变更**(2026-09-29):令牌来源由「只读 `.env`」改为**两处取并集**
  - `.env` 的 `AGENT_API_TOKEN`(逗号分隔):以明文存放,直接 `compare_digest` 比对
  - 数据库 `agent_tokens` 表:只存哈希,比对 `sha256(明文) == 已存哈希`
  - 新增 `session: Session = Depends(get_db)` 参数用于读取库内令牌
    (与路由自身的 `get_db` 是同一依赖,FastAPI 每请求只解析一次,复用同一会话)
- **返回**:`AgentPrincipal(fingerprint=...)`;指纹均为 `sha256(明文)[:16]`,
  与 `app/token_store.py::fingerprint_of_hash` 一致,审计日志 `agent:<指纹>` 可直接对账
- **404 语义不变**:两个来源都没有任何令牌时,`/api/agent` 整体返回 404 而不是 401
- **撤销即时生效**:令牌被撤销/停用后从 `enabled_hashes` 中消失,下一次请求即 401
  (若因此一枚不剩,则回到 404)
- **副作用**:每个 Agent 请求多一次索引查询(仅启用令牌的哈希列)
- **测试**:`tests/test_agent_tokens.py::test_db_token_authorizes_agent_requests`、
  `::test_db_token_and_env_token_coexist`、`::test_revoked_token_stops_working_immediately`、
  `::test_revoking_every_token_hides_the_endpoint_again`

## 新增

### `local_time_filter(value, tz_name="UTC", fmt="%Y-%m-%d %H:%M") -> str`

- **位置**:`app/deps.py:33`,注册为模板过滤器 `localtime`(`app/deps.py:49`)
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
