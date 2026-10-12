# `app/feed_tokens.py`

本模块管理只读 ICS 订阅令牌。令牌有**两条来源**:

- **每个账号一条固定地址**:
  `token = sisi_feed_ + HMAC(服务端密钥, "feed:<账号id>")[:40]`。账号创建时(以及启动引导
  回填)就登记一行 `feed_tokens`(标签 `ACCOUNT_FEED_LABEL`,即「账号订阅地址(固定)」),
  **永不变更**;因为服务端随时能重新算出来,界面可以长期展示、验证通过后可以邮件告知,
  而库里仍然只存 `sha256`。代价:轮换 `data/session_secret` 会让所有固定地址失效。
- **临时地址**(`create_feed_token`):随机生成、明文只显示一次,供「换手机」
  「链接外泄」时单独停用/撤销。

范围语义:`user_id` 有值 → 该成员**可见范围**(个人 + 共享)的合并日历;
`user_id` 为 `NULL` → **家庭共享日历**范围,专门给没有账号的外部订阅者(客厅平板、长辈)。
升级前的**无令牌地址 `/calendar` 与 `/calendar.ics` 已经取消**,不再服务首位管理员的可见范围
(见 `app/routers/feed.py` 与 `AGENTS.md` 不变量 14)。

安全基调:**库内只存 `sha256`**(与 `app/token_store.py` 的 Agent 令牌同一套路)。
**固定地址是唯一例外**:它的明文可以由服务端重新算出来,所以页面可以长期显示;
但数据库里仍然只有 `token_hash`,任何地方都不得把明文写进日志或审计
(`AGENTS.md` 不变量 18、23)。

## 常量

### `FEED_TOKEN_PREFIX` / `MAX_LABEL` / `ACCOUNT_FEED_LABEL` / `ACCOUNT_TOKEN_HEX`

- **位置**:`app/feed_tokens.py:33`–`app/feed_tokens.py:38`
- **取值与含义**:
  - `FEED_TOKEN_PREFIX = "sisi_feed_"`:明文前缀,**只为人眼辨识,不参与鉴权**;
  - `MAX_LABEL = 100`:令牌名称长度上限;
  - `ACCOUNT_FEED_LABEL = "账号订阅地址(固定)"`:固定地址那一行的记录名,
    界面与文档都按它识别(建号/回填时写入,`ensure_account_feed_token` 每次都把它纠回来);
  - `ACCOUNT_TOKEN_HEX = 40`:派生令牌截取的 hex 字符数(40 hex = 160 bit,足以抵抗穷举)。
- **不变量**:前缀字符串本身不是秘密,不得把它当作校验条件;
  `ACCOUNT_FEED_LABEL` 是**用户可见判据**,改文案等于改行为
  (`tests/test_family_accounts.py::test_feed_tokens_are_listed_per_scope` 断言它排在列表第一位)

## 固定地址一族(2026-10-12 新增)

### `account_feed_token(settings, user_id) -> str`

- **位置**:`app/feed_tokens.py:41`
- **用途**:算出账号那条**固定**订阅令牌的明文(不是随机数,而是服务端派生)
- **参数**:`user_id` 是账号 id(空值会算出一个无意义的令牌,调用方必须传真实 id);
  密钥取自 `settings.load_or_create_session_secret()`
- **返回**:`f"{FEED_TOKEN_PREFIX}{HMAC-SHA256(密钥, 'feed:<user_id>').hexdigest()[:40]}"`
- **副作用**:可能触发 `load_or_create_session_secret()` 创建 `data/session_secret`(首次运行)
- **不变量**:
  - 同一个账号**永远**得到同一个明文,且不同账号互不相同;
  - 派生载荷格式 `feed:<user_id>` **不得改动**——改了等于一次性换掉所有人的地址;
  - 明文**不得**写日志、写审计、写数据库(库里只有 `account_feed_hash`)
- **测试**:`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`、
  `tests/test_public_prefix.py::test_subscription_url_comes_from_public_base_url`、
  `tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`、
  `tests/test_family_mail.py::test_owner_recipient_gets_his_own_fixed_address`、
  `::test_member_recipient_gets_only_his_own_address`
- **调用方**:`app/feed_tokens.py:56`(`account_feed_hash`)、`:106`(`account_subscription_url`)、
  `app/mailqueue.py:273`(邮件正文里的地址)、`app/routers/pages.py:590`(界面展示)

### `account_feed_hash(settings, user_id) -> str`

- **位置**:`app/feed_tokens.py:55`
- **用途**:固定地址在库里的形态(`sha256(明文)`,hex)
- **返回**:`app/token_store.py::hash_token(account_feed_token(settings, user_id))`
- **副作用**:同 `account_feed_token`(可能创建密钥文件)
- **不变量**:库内**只**出现这个哈希;比对必须走 `hmac.compare_digest`
- **测试**:暂无直接测试(`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`
  断言 `row.token_hash == hash_token(first)`,等价覆盖)

### `fixed_account_token(session, settings, *, user_id) -> FeedToken | None`

- **位置**:`app/feed_tokens.py:59`
- **用途**:找出账号那条固定地址的**记录**(只读,不新建)
- **参数**:`user_id` 是关键字参数(必须显式写,避免与随机令牌搞混)
- **返回**:命中的 `FeedToken`,或 `None`(该账号还没有固定地址记录)
- **副作用**:无(只查库)
- **不变量**:用 `hmac.compare_digest` 在**该账号自己的**令牌列表里逐行比对
  (`list_feed_tokens(session, user_id=user_id)`),因此绝不会把别人的固定地址认成本人的;
  查不到时**不得**顺手补建——补建只属于 `ensure_account_feed_token`
- **测试**:`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`、
  `tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`
- **调用方**:`app/feed_tokens.py:74`(`ensure_account_feed_token`)、
  `app/routers/pages.py:576`(给界面标出哪一行是固定地址)

### `ensure_account_feed_token(session, settings, user) -> FeedToken`

- **位置**:`app/feed_tokens.py:70`
- **用途**:保证账号有一条固定订阅地址,返回它的记录;**幂等**
- **行为**:
  - 已有记录 → 若被停用则重新 `enabled = True`,若标签不是 `ACCOUNT_FEED_LABEL` 则改回来,
    刷新 `updated_at`,直接返回原行(**不新建**);
  - 没有 → 建一行 `user_id=user.id` / `label=ACCOUNT_FEED_LABEL` /
    `token_hash=account_feed_hash(settings, user.id)` / `enabled=True`。
- **返回**:`FeedToken`(**总是**返回一行)
- **副作用**:`session.add` + `flush`(事务由调用方提交);可能更新既有行
- **不变量**:
  - 幂等:重复调用**绝不多出第二条**固定地址;固定地址**不能被停用**,
    所以这里要顺手把 `enabled` 纠回 `True`;
  - 它是**每条建号路径的必需步骤**,漏掉会让界面显示不出固定地址:
    `app/accounts.py:244`(`create_user`)、`app/bootstrap.py:136`(`ensure_family_setup` 回填
    升级上来的老账号)
- **测试**:`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`
  (重复确保仍是同一行、该账号列表长度仍为 1)、
  `::test_feed_tokens_are_listed_per_scope`(建号即有,排在列表第一位)
- **调用方**:`app/accounts.py:244`、`app/bootstrap.py:136`

### `is_fixed_account_token(session, settings, token) -> bool`

- **位置**:`app/feed_tokens.py:97`
- **用途**:判断这条记录是不是账号的固定地址(界面据此**隐藏停用/撤销**)
- **返回**:`token.user_id` 为空 → `False`;否则用 `hmac.compare_digest` 比对
  `token.token_hash` 与 `account_feed_hash(settings, token.user_id)`
- **副作用**:无
- **不变量**:判断依据是「哈希等于该账号的派生哈希」,**不是**标签文案——
  标签可被改名(`rename`),按标签判断会让人把固定地址当成临时令牌撤销掉
- **测试**:`tests/test_family_accounts.py::test_account_feed_token_is_stable_and_derived`、
  `tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`
- **调用方**:`app/routers/pages.py:683`(启停)、`:734`(撤销)——
  两处命中都返回 **409** 而不是 404

### `account_subscription_url(session, settings, user) -> str`

- **位置**:`app/feed_tokens.py:104`
- **用途**:账号的固定订阅地址(**绝对地址**,可直接粘贴到 iPhone)
- **参数**:`session` 当前未使用(保留签名以便调用方与其它 `*_for` 函数保持一致);
  地址完全由 `settings` 与 `user.id` 决定
- **返回**:`app/urls.py::subscription_url(settings, account_feed_token(settings, user.id))`,
  形如 `https://<节点>/calendar/sisi_feed_xxx.ics`
- **副作用**:可能创建密钥文件(见 `account_feed_token`)
- **不变量**:地址**永远**由服务端现算,不依赖库里的明文、也不依赖「只显示一次」的机制;
  正文/页面里不得出现别人的地址
- **测试**:`tests/test_email_verify.py::test_full_verification_flow_mails_the_fixed_address`
  (第二封信正文里的地址就是它)
- **调用方**:`app/routers/auth.py:503`(验证通过后的补发信)、
  `app/routers/pages.py:423`(「我的设置」里的固定地址)

### 界面约束:固定地址不可停用 / 不可撤销

- **位置**:`app/routers/pages.py:683`(toggle)、`:734`(delete)
- **行为**:命中 `is_fixed_account_token` → 返回 **409** 与中文原因,
  数据库**保持原样**(`enabled` 仍为 `True`),审计也不记
- **理由**:它是订阅端(iPhone)长期填着的那条地址;允许停用等于让家人自己把日历刷没,
  而固定地址本来就可以随时重新展示、重新发信,没有停用的必要。
  需要一条能单独撤销的地址时,**另建临时令牌**(`create_feed_token`)
- **不变量**:固定地址**不提供**停用/撤销入口(409),但它**可以**重新显示、重新发信;
  临时令牌的停用/撤销语义不变
- **测试**:`tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`
  (两次请求都是 409,且库里那行 `enabled` 仍为 `True`)、
  `::test_my_feed_is_stable_and_temporary_token_shown_once`
  (固定地址刷新后仍然可见,临时地址明文只在创建那一次出现)

## 其余令牌函数

### `create_feed_token(session, *, user_id, label) -> tuple[str, FeedToken]`

- **位置**:`app/feed_tokens.py:118`
- **用途**:生成一枚**临时**订阅令牌,返回一次性明文与记录
- **参数**:
  - `user_id`:**非空 = 该账号可见范围;`None`/空串 = 家庭共享范围**(入库时统一成 `None`)
  - `label`:去空白后不得为空、≤`MAX_LABEL`,否则抛 `SettingValidationError`
- **返回**:`(明文, FeedToken)`;明文形如 `sisi_feed_<43 字符>`
- **副作用**:`session.add` + `flush`(事务由调用方提交);`enabled=True`、`last_used_at=None`
- **不变量**:明文**不得**写日志、不得在后续列表页回显(页面只显示指纹);
  库内只有 `token_hash`;标签不得写成 `ACCOUNT_FEED_LABEL`(否则界面会把它当固定地址)
- **测试**:`tests/test_family_accounts.py::test_feed_token_roundtrip`、
  `::test_family_feed_token_has_no_user`、`::test_subscription_url_uses_configured_base`、
  `tests/test_family_pages.py::test_my_feed_is_stable_and_temporary_token_shown_once`

### `list_feed_tokens(session, *, user_id) -> list[FeedToken]`

- **位置**:`app/feed_tokens.py:137`
- **用途**:列出**某个范围**的令牌
- **参数**:`user_id` 非空 → 该账号的令牌;为空 → **`user_id IS NULL` 的家庭范围令牌**
  (两个范围互不混淆)
- **返回**:按 `created_at.asc(), id.asc()` 排序;因此建号时创建的那条固定地址
  总是排在**第一位**
- **副作用**:无
- **不变量**:成员只能看到自己的令牌,家庭范围令牌只出现在管理员面;
  列表内容不含明文
- **测试**:`tests/test_family_accounts.py::test_feed_tokens_are_listed_per_scope`、
  `tests/test_family_pages.py::test_member_cannot_touch_another_users_feed_token`

### `enabled_feed_hashes(session) -> list[tuple[str, str | None]]`

- **位置**:`app/feed_tokens.py:148`
- **用途**:一次取出全部**启用**令牌的 `(哈希, user_id)`,供需要常量时间比对的调用方使用
- **返回**:元组列表;不发任何明文
- **副作用**:无
- **不变量**:必须只含启用中的令牌(停用的临时令牌立即消失);
  固定地址不受停用影响(它本来就不允许停用),所以它**始终**在列表里
- **测试**:`tests/test_family_accounts.py::test_disabled_feed_token_cannot_be_resolved`
  (停用的临时令牌不在列表里,而固定地址仍可解析)
- **说明**:当前订阅入口用的是逐个比对的 `resolve_feed_token`,应用侧暂无调用方

### `resolve_feed_token(session, plaintext) -> FeedToken | None`

- **位置**:`app/feed_tokens.py:160`
- **用途**:按明文找出**启用中**的令牌记录——订阅入口的鉴权处
- **行为**:先 `sha256`,再对 `enabled IS TRUE` 的每一行做 `hmac.compare_digest` 常量时间比对
- **参数**:空明文直接返回 `None`
- **返回**:命中的 `FeedToken`,或 `None`(不存在、已停用一律 `None`,不区分原因)
- **副作用**:无
- **不变量**:只比哈希、不比明文;数量只有个位数,允许线性扫描,但**必须**用常量时间比对;
  `None` 即「地址无效」,由订阅路由转成 404;固定地址与临时地址走的是**同一条**解析路径
- **测试**:`tests/test_family_accounts.py::test_feed_token_roundtrip`(错误明文返回 `None`)、
  `::test_disabled_feed_token_cannot_be_resolved`、
  `tests/test_family_web.py::test_invalid_feed_token_returns_404`、
  `::test_disabled_feed_token_returns_404`

### `get_feed_token(session, token_id) -> FeedToken | None`

- **位置**:`app/feed_tokens.py:172`
- **用途**:按 id 取令牌(启停/改名/撤销前先取)
- **返回**:`FeedToken` 或 `None`
- **副作用**:无
- **不变量**:取到之后**必须**校验它属于当前账号或属于家庭范围,
  否则成员就能操作别人的令牌
- **测试**:`tests/test_family_pages.py::test_member_cannot_touch_another_users_feed_token`
  (别人的令牌启停/撤销都是 404)

### `set_enabled(session, token, enabled) -> None`

- **位置**:`app/feed_tokens.py:176`
- **用途**:停用/重新启用一枚**临时**令牌
- **副作用**:写 `enabled`、`updated_at` 并 `flush`
- **不变量**:停用即时生效:**下一次**订阅请求即 404(不需要重启、不需要清理缓存),
  但令牌记录与已生成的订阅文件都保留;重新启用后同一地址又可用;
  **固定地址不得走这条路径**(调用方先判 `is_fixed_account_token` 并返回 409)
- **测试**:`tests/test_family_accounts.py::test_disabled_feed_token_cannot_be_resolved`、
  `tests/test_family_web.py::test_disabled_feed_token_returns_404`、
  `tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`

### `rename(session, token, label) -> None`

- **位置**:`app/feed_tokens.py:182`
- **用途**:改令牌名称(校验规则同 `create_feed_token`)
- **副作用**:写 `label`、`updated_at` 并 `flush`
- **不变量**:改名**不改变**明文、哈希、范围与订阅地址;固定地址**也不怕改名**——
  它靠 `is_fixed_account_token` 按哈希识别,下次 `ensure_account_feed_token`
  还会把标签纠回 `ACCOUNT_FEED_LABEL`
- **测试**:暂无直接测试

### `delete(session, token) -> None`

- **位置**:`app/feed_tokens.py:188`
- **用途**:撤销**临时**令牌(**硬删除**)
- **副作用**:删除 `feed_tokens` 一行并 `flush`
- **不变量**:撤销后明文再也无法解析,地址立即 404;
  **已经生成在 `export/feeds/` 下的订阅文件不会被删除**(导出只覆盖当前目标,没有清理旧目标的逻辑),
  撤销的是访问路径而不是文件——`export/feeds/` 不在任何 Web 挂载点下,
  因此仍以「文件系统权限」而非「HTTP 可达性」保护;
  **固定地址不得走这条路径**(调用方先判 `is_fixed_account_token` 并返回 409)
- **测试**:`tests/test_family_pages.py::test_fixed_address_cannot_be_disabled_or_revoked`
  (固定地址的撤销返回 409);临时令牌的撤销由页面入口调用,
  跨账号撤销被拒见 `::test_member_cannot_touch_another_users_feed_token`

### `touch(session, token) -> None`

- **位置**:`app/feed_tokens.py:193`
- **用途**:记录最近一次被订阅端拉取的时间
- **副作用**:写 `last_used_at` 并 `flush`(订阅入口随后 `session.commit()`)
- **不变量**:`last_used_at` **只用于展示**,不得参与鉴权或判断令牌是否有效
- **测试**:`tests/test_family_web.py::test_feed_token_is_touched_on_access`

### `default_token(session, *, user_id) -> FeedToken | None`

- **位置**:`app/feed_tokens.py:199`
- **用途**:取该范围里第一枚**启用**令牌,用于界面直接展示一条可用订阅地址
- **返回**:`FeedToken` 或 `None`(该范围没有任何启用令牌)
- **副作用**:无
- **不变量**:不得返回已停用的令牌
- **测试**:`tests/test_family_accounts.py::test_family_feed_token_has_no_user`
- **说明**:当前页面走 `list_feed_tokens` 全量渲染,应用侧暂无调用方

### `feed_scope_label(user_id) -> str`

- **位置**:`app/feed_tokens.py:207`
- **用途**:给范围一个人可读的短标签
- **返回**:`user_id` 为空 → `"家庭共享日历"`(`FAMILY_FEED_LABEL`);非空 → `"个人日历"`
- **副作用**:无
- **不变量**:范围内涵由「有没有 `user_id`」决定;非空时返回的是**范围类型名**
  (该账号可见范围),不是账号名
- **测试**:`tests/test_family_accounts.py::test_family_feed_token_has_no_user`

### `subscription_url_for(settings, plaintext) -> str`

- **位置**:`app/feed_tokens.py:211`
- **用途**:把令牌明文拼成可直接在 iPhone 里订阅的完整地址
- **参数**:`settings.public_base_url` 既是公开面的挂载前缀(如 `https://host/calendar`),
  也是拼地址的基地址;会去空白与末尾 `/`。**基地址为空时**地址退化成 `/<令牌>.ics`
  (裸路径),没有回退常量
- **返回**:`"{基地址}/{明文}.ics"`,即形如 `https://host/calendar/sisi_feed_xxx.ics`
- **副作用**:无
- **不变量**:地址必须与 `PUBLIC_BASE_URL` 的挂载前缀一致(Tailscale Serve 上只有
  那一个 `/calendar` 挂载,且本仓库**不做前缀剥离**);地址里除 `.ics` 后缀不带任何
  多余查询参数(避免 iPhone 侧被当成 HTML 页面);实现委托给 `app/urls.py::subscription_url`,
  不得另写一套拼接
- **测试**:`tests/test_family_accounts.py::test_subscription_url_uses_configured_base`、
  `tests/test_family_pages.py::test_my_feed_is_stable_and_temporary_token_shown_once`
  (创建响应里带完整 `/<明文>.ics` 地址)

### `subscription_path_for(plaintext) -> str`

- **位置**:`app/feed_tokens.py:221`
- **用途**:站内相对路径(**不带公开前缀**),便于页面展示、内部跳转与测试断言
- **参数**:`plaintext` 已带 `.ics` 后缀时先剥掉,再统一补回
- **返回**:固定形态 `/<明文>.ics`(如 `/sisi_feed_xxx.ics`)——
  **不再**带 `/calendar` 前缀:公开前缀是请求级状态,写死会让裸路径入口渲染出错误链接
- **副作用**:无
- **不变量**:形态必须与订阅路由 `app/routers/feed.py::member_calendar_feed`
  (`/{token}.ics`,后缀由 `app/routers/feed.py:125` 的 `_token_or_404` 剥掉)一致;
  需要绝对地址时用 `subscription_url_for` / `account_subscription_url`
- **测试**:`tests/test_family_accounts.py::test_subscription_url_uses_configured_base`
  (断言 `subscription_path_for(plaintext) == f"/{plaintext}.ics"`)

## 订阅入口的行为(与本模块配套)

> 订阅入口的完整契约另见 `docs/static/app-routers-feed.md`;此处只列与本模块相关的部分。

- **位置**:`app/routers/feed.py::member_calendar_feed`(`app/routers/feed.py:136`)
- **地址**:`/{令牌}.ics`(同时挂在裸路径与公开前缀下,所以对外的完整地址是
  `<公开基地址>/<令牌>.ics`)
- **流程**:`resolve_feed_token` 成功 → `_ensure_current` 确保该范围的订阅文件存在
  (新令牌首次访问会触发补生成,否则订阅端会拿到 503)→ `touch` + `commit` →
  返回 `feed_path_for(settings, user_id=row.user_id)` 指向的文件
  (账号令牌 → `export/feeds/user-<账号id>.ics`,家庭令牌 → `export/feeds/family.ics`)
- **404 语义**:令牌无效或已停用返回 **404**(纯文本 + `Cache-Control: no-store`),
  **不做 HTML 重定向**,避免 iPhone 把错误页存成日历地址;
  有效性不做任何缓存,因此停用后立即 404
- **无令牌旧地址已取消**:`/calendar`(前缀本身)**不再**提供日历——日历客户端拿到
  404 纯文本,浏览器拿到 303 到 `/calendar/admin`;`/calendar.ics` 也不再注册
- **测试**:`tests/test_family_web.py::test_member_feed_contains_only_their_visible_events`、
  `::test_family_feed_excludes_personal_calendars`、`::test_invalid_feed_token_returns_404`、
  `::test_disabled_feed_token_returns_404`、`::test_feed_token_only_sees_its_own_file`、
  `::test_owner_fixed_address_serves_owner_scope_not_union`、
  `::test_legacy_tokenless_address_is_gone`、
  `tests/test_public_prefix.py::test_prefix_root_serves_text_404_to_calendar_clients`

## 变更

本模块是 2026-10 家庭版改造中**新增**的文件。它带来的既有模块变化:

- `app/urls.py`(2026-10-12 新增):所有地址拼装集中在这里,本模块的
  `subscription_url_for` 与 `account_subscription_url` 都委托给它;
- `app/routers/feed.py`:订阅入口是 `/{令牌}.ics`,同时挂在裸路径与公开前缀下;
  **`/calendar` 与 `/calendar.ics` 不再提供日历**(此前它们服务首位管理员的可见范围);
- `app/exporter.py`:新增 `feed_target_key` / `feed_path_for`,订阅文件按目标落盘到
  `export/feeds/`(`user-<账号id>.ics` 与 `family.ics`),不再只有一个 `calendar.ics`
  对外;版本号与内容哈希仍由全库并集决定;
- `app/accounts.py::create_user` 与 `app/bootstrap.py::ensure_family_setup`:
  建号/回填时调 `ensure_account_feed_token`,保证每个账号都有固定地址;
- `app/routers/pages.py`:成员在「我的设置」里查看**固定地址**(长期显示)、
  创建/停用/撤销**临时**令牌与家庭范围令牌列表;固定地址的启停/撤销返回 409;
- `app/accounts.py::delete_user`:删账号时连带删除其订阅令牌;
- 数据模型:`feed_tokens` 由迁移 0005 建出(见 `docs/static/migrations-0005.md`)。

## 变更记录

- 2026-10-08:新增本文档。归档订阅令牌的「只存哈希、明文一次性」基调、
  `user_id=None` 表示家庭共享范围、`subscription_url_for`/`subscription_path_for` 的
  `/calendar/<令牌>.ics` 地址形态,以及令牌停用后立即 404 的行为。
- 2026-10-12:补齐**固定地址一族**——`account_feed_token` / `account_feed_hash` /
  `fixed_account_token` / `ensure_account_feed_token` / `is_fixed_account_token` /
  `account_subscription_url` 与常量 `ACCOUNT_FEED_LABEL` / `ACCOUNT_TOKEN_HEX`,
  以及「固定地址不可停用/撤销(409)、但可随时重新显示与重新发信」的界面约束。
  同时修正过期说法:`subscription_url_for` 的参数改为 `settings.public_base_url`
  (不再是 `settings.subscription_url`,也不再回退常量 `/calendar`),
  `subscription_path_for` 返回 `/<令牌>.ics`(不带 `/calendar` 前缀),
  订阅入口测试名更新为 `test_owner_fixed_address_serves_owner_scope_not_union` /
  `test_legacy_tokenless_address_is_gone`,并声明**无令牌旧地址已彻底取消**;
  顺带把全文行号对齐到源码。