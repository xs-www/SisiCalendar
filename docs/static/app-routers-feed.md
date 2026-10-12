# `app/routers/feed.py`

只读订阅入口(公开,无鉴权)。**本轮(2026-10-12)只剩一条带令牌的路由**:

| 路由 | 函数 | 位置 | 服务范围 |
|---|---|---|---|
| `GET\|HEAD /{token}.ics`(挂裸路径与 `base_path` 两处) | `member_calendar_feed` | `app/routers/feed.py:133`(函数 `:136`) | 令牌解析出的范围 |

路由路径是**相对的** `/{token}.ics`,由 `app/main.py` 同时挂到裸路径与
`PUBLIC_BASE_URL` 的路径前缀下(见 `docs/static/app-main.md`),因此对外地址永远是
`<公开基地址>/<令牌>.ics`。后缀限定 `.ics`,这样 `/calendar/login`、`/calendar/admin/...`
不会被订阅路由抢走。

> **升级前的无令牌地址已永久取消**:`/calendar`、`/calendar/`(前缀本身)由
> `app/routers/pages.py::public_root` 处理 —— 对日历客户端是 **404 纯文本 +
> `Cache-Control: no-store`**,对浏览器是 **303 → `/calendar/admin`**;
> `/calendar.ics` 会落到本路由(令牌名恰好是 `calendar`)→ 解析不到 → **404 纯文本**。
> 它们**绝不**退化成全库并集,也不再是「首位管理员的可见范围」那个兼容别名
> (`AGENTS.md` 不变量 14)。
>
> 令牌明文只出现在 URL 里。订阅入口**不做 HTML 重定向**,错误也返回纯文本文案,
> 避免 iPhone 把错误页存成日历地址。

## 辅助函数

### `_ensure_current(session, settings, *, path) -> None`

- **位置**:`app/routers/feed.py:42`
- **用途**:在返回订阅文件前补生成 ICS。**三种情况都必须重新生成**:
  1. 全局导出状态 `dirty`;
  2. 内部并集文件 `settings.calendar_path` 不存在;
  3. **本次要返回的 `path` 不存在**——新账号或新令牌刚创建时它还没生成,
     即使全局不 dirty 也必须补,否则订阅端会拿到 503
- **副作用**:调用 `app/exporter.py::export_calendar` 并提交事务(原子替换发布文件)
- **失败处理**:导出抛 `ExportError` 时回滚并记一条 warning,**继续返回最近的有效文件**;
  其他异常也回滚并只记异常类型名——订阅入口不得因数据库问题中断
- **不变量**:导出失败绝不能把事件数据弄丢,也不能把半截文件暴露出去
  (与 `AGENTS.md` 不变量 4、5 一致)
- **测试**:`tests/test_feed.py::test_dirty_state_is_repaired_by_feed_request`、
  `::test_feed_serves_last_valid_file_when_export_fails`、
  `tests/test_family_mail.py::test_feed_files_exist_for_every_target_after_export`

### `_is_not_modified(request, etag, modified) -> bool`

- **位置**:`app/routers/feed.py:62`
- **用途**:条件请求判定。`If-None-Match` 存在时**不再看** `If-Modified-Since`(RFC 9110);
  支持通配 `*`;时间比较精确到秒
- **返回**:命中条件返回 `True`(调用方回 304)
- **副作用**:无
- **测试**:`tests/test_feed.py::test_conditional_get_with_etag_returns_304`、
  `::test_conditional_get_with_last_modified_returns_304`、
  `::test_stale_last_modified_returns_full_body`

### `_serve_feed(request, *, path, settings, missing_message) -> Response`

- **位置**:`app/routers/feed.py:83`
- **用途**:读文件并构造响应,统一缓存头
- **返回与状态码**:
  - 文件仍不存在 → **503**、`text/plain`、`Cache-Control: no-store`
  - 条件命中 → **304**(仍带 ETag/Last-Modified/Cache-Control)
  - 正常 → **200**;**HEAD** 返回相同响应头与 `Content-Length`,正文为空
- **响应头**:
  - `Content-Type: text/calendar; charset=utf-8`
  - `ETag: "<sha256(文件内容)>"`(内容哈希,**不是**版本号)
  - `Last-Modified`:文件 mtime(UTC、GMT 格式)
  - `Cache-Control: private, max-age=<settings.feed_cache_seconds>, must-revalidate`
  - `Content-Disposition: inline; filename="calendar.ics"`
  - `X-Content-Type-Options: nosniff`
- **不变量**:缓存头**必须**是 `private`——这是一份私人日历,不能被共享缓存留存
- **测试**:`tests/test_feed.py::test_feed_headers`、`::test_etag_matches_content_hash`、
  `::test_head_returns_headers_without_body`、
  `tests/test_page_security.py::test_feed_is_not_served_with_admin_csp`

### `_token_or_404(raw_token) -> str`

- **位置**:`app/routers/feed.py:125`
- **用途**:去空白,并**兜底**剥掉可能带的 `.ics` 后缀
  (路由形状已固定为 `/{token}.ics`,正常路径参数里不会带后缀;保留这层是为了容错)
- **返回**:纯令牌明文;函数名里的 `404` 只是说明后续解析失败会 404

## 路由契约

### `GET|HEAD /{token}.ics` — `member_calendar_feed`

- **位置**:`app/routers/feed.py:136`;装饰器 `app/routers/feed.py:133`
  (`methods=["GET", "HEAD"]`,summary「按成员订阅日历」)
- **参数**:`token`(路由参数,即令牌明文;去掉 `.ics` 后查库)
- **行为**:
  1. `app/feed_tokens.py::resolve_feed_token(session, plaintext)` 解析令牌
     (逐个 `hmac.compare_digest` 比对哈希,只认**启用**行);
  2. 解析不到 → **404** `text/plain` + `Cache-Control: no-store`,
     文案「订阅地址无效或已被停用,请在日历应用的设置里重新获取。」,
     不重定向、不暴露是否存在其它令牌;
  3. `_ensure_current(...)` 按 `feed_path_for(settings, user_id=row.user_id)` 补生成;
  4. `app/feed_tokens.py::touch(session, row)` 记 `last_used_at` 并提交
     (失败只回滚 + 记日志,订阅照常返回);
  5. `_serve_feed(...)` 返回文件。
- **返回**:200 ICS / 304 / 404 / 503
- **测试**:`tests/test_feed.py::test_feed_is_public_and_not_a_redirect`、
  `tests/test_family_web.py::test_invalid_feed_token_returns_404`、
  `::test_disabled_feed_token_returns_404`、
  `::test_feed_token_is_touched_on_access`

## 范围语义(必须保持)

- **成员令牌**(`feed_tokens.user_id` 非空):服务该账号**可见日历**的合并结果,
  落盘为 `export/feeds/user-<账号id>.ics`(`app/exporter.py::feed_path_for`)。
- **家庭令牌**(`user_id` 为空):服务 `export/feeds/family.ics`,**只含共享日历**,
  不含任何人的私人日程;给没有账号的外部订阅者用。
- **每个账号一条固定地址**(`AGENTS.md` 不变量 23):
  `token = sisi_feed_ + HMAC(session_secret, "feed:<user_id>")[:40]`,
  由服务端密钥**派生**而非随机生成,因此**同一个账号永远得到同一条地址**;
  账号创建时(`app/accounts.py::create_user(settings=...)`)与启动引导回填
  (`app/feed_tokens.py::ensure_account_feed_token`,幂等)各登记一行 `feed_tokens`,
  标签固定为 `ACCOUNT_FEED_LABEL = "账号订阅地址(固定)"`,库里**只存 sha256**。
  它和其它令牌的区别:
  - **界面不给停用/撤销**:`my-feed` 的 toggle/delete 命中固定行返回 **409**;
  - **可以随时重新显示**:页面能重新算出明文(`app/feed_tokens.py::account_subscription_url`),
    所以「明文只在创建时出现一次」这条规则**对它不成立**;
  - 验证通过后由 `app/email_verify.py::send_subscription_mail` 再发一封通知信;
  - **轮换 `data/session_secret` 会让所有固定地址失效**(同时让所有验证码哈希失效)。
- **临时令牌**(`create_feed_token`)仍是随机生成、明文只在创建响应里出现一次,
  可以单独停用/撤销——固定地址不受影响。
- **绝不退化成全库并集**:`export/calendar.ics` 只是内部一致性产物(版本与内容哈希用),
  任何对外地址都不得直接服务它。
- 若目标文件确实生成不出来(例如库里没有任何账号),`_serve_feed` 返回 **503** 纯文本。

## 测试

- 范围:`tests/test_family_web.py::test_member_feed_contains_only_their_visible_events`、
  `::test_family_feed_excludes_personal_calendars`、
  `::test_owner_fixed_address_serves_owner_scope_not_union`(首位管理员的**固定**地址
  只给他自己的可见范围,不含别人的私人日程)、
  `::test_legacy_tokenless_address_is_gone`(`/calendar`、`/calendar/`、`/calendar.ics`
  一律纯文本 404)、
  `::test_feed_token_only_sees_its_own_file`、
  `::test_invalid_feed_token_returns_404`、`::test_disabled_feed_token_returns_404`、
  `::test_feed_token_is_touched_on_access`
- 协议:`tests/test_feed.py::test_feed_is_public_and_not_a_redirect`、
  `::test_legacy_tokenless_paths_are_gone`、`::test_etag_changes_after_new_event`、
  `tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`
  (前缀根:机器 404 纯文本、浏览器 303 → `/calendar/admin`)、
  `::test_subscription_url_comes_from_public_base_url`
- 固定地址(领域层):`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`、
  `::test_feed_tokens_are_listed_per_scope`(固定行永远排在列表第一位)、
  `tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`(409)、
  `::test_my_feed_is_stable_and_temporary_token_shown_once`(固定地址刷新后仍在,
  临时地址明文只出现一次)
- 令牌哈希:`tests/test_family_accounts.py::test_feed_token_roundtrip`、
  `::test_disabled_feed_token_cannot_be_resolved`

## 找不到对应测试的点

- 「目标文件缺失 → 请求时即时补生成」这条分支**没有**直接测试
  (被删除的是 `settings.calendar_path` 而非某个 `feeds/*.ics`;
  最接近的是 `test_feed.py::test_dirty_state_is_repaired_by_feed_request`
  与 `test_family_mail.py::test_feed_files_exist_for_every_target_after_export`)。
- `_serve_feed` 的 **503 分支**没有专门测试。
- 「轮换 `session_secret` 后固定地址失效」没有回归测试(它是刻意的取舍)。

## 变更记录

- 2026-10-08:家庭版新增本文件。归档 `/calendar/{token}` 的令牌解析与按账号/家庭范围、
  旧地址改绑首位管理员(不再服务并集)、`_ensure_current` 的目标文件缺失补生成、
  缓存头与 304/503/404 语义。
- 2026-10-12:旧无令牌地址**彻底取消**。本模块只剩
  `@router.api_route("/{token}.ics", methods=["GET", "HEAD"])` 一条路由,
  旧的 `/calendar`、`/calendar.ics` 处理器已删除(前缀本身改由
  `app/routers/pages.py::public_root` 处理);补写「每个账号一条由服务端密钥派生、
  永不变更的固定地址」的语义(409 不可停用/撤销、可重复显示与重发、
  明文一次性规则对它不成立),并把测试名更新为 `test_owner_fixed_address_serves_owner_scope_not_union` /
  `test_legacy_tokenless_address_is_gone` / `test_fixed_address_cannot_be_disabled_or_revoked` /
  `test_my_feed_is_stable_and_temporary_token_shown_once`。