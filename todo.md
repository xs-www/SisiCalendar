# TODO

## 本轮已完成(2026-10-14:GitHub Actions 自动构建镜像 → GHCR)

需求:让 `master` 的提交自动构建成 Docker 镜像,NAS 直接 `pull` 就能部署与升级。计划文档
[`docs/CI镜像自动发布计划.md`](docs/CI镜像自动发布计划.md)(已实施),链路契约
[`docs/static/ci-docker-publish.md`](docs/static/ci-docker-publish.md)。

- [x] **新增 CI 流水线** `.github/workflows/docker-publish.yml`:`quality` job 先跑
      `uv sync --frozen` + `ruff check app tests migrations` + `pytest -q` 门禁,
      `needs: quality` 的 `image` job 再构建 `linux/amd64` + `linux/arm64` 并推送到
      `ghcr.io/xs-www/sisicalendar`(标签 `latest` / `master` / `sha-<7位>`;`v*` tag 出语义化版本号)
- [x] **Action 主版本用 GitHub API 核过(2026-10-14)**:checkout v7、setup-uv v10、
      setup-qemu v4、setup-buildx v4、login v4、metadata v6、build-push v7
- [x] **首次运行排错**:`astral-sh/setup-uv@v10` 无法解析(`v10` 不是该仓库的浮动标签,
      最高浮动只到 `v7.6`)→ 改为钉精确版本 `@v10.3.0`;其余六个 Action 的主版本标签
      已逐个用 `git/ref/tags` 接口验过存在
- [x] **不引入任何自定义 secret**:登录 GHCR 用自带的 `GITHUB_TOKEN`(`packages: write`
      只给 `image` job;顶层 `permissions: contents: read`)
- [x] **照顾老 NAS**:`provenance: false` / `sbom: false`(attestation 清单会让部分老设备报
      `manifest unknown`);PR 上只构建不推送
- [x] **新增 `compose.nas.yaml`(纯拉取)**:`image:` 替代 `build:`,服务名仍为 `calendar`,
      端口只绑 `127.0.0.1:18443:8000`,资源上限 / 健康检查 / `no-new-privileges` / 日志轮转
      与 `compose.yaml` 逐字对齐;**现有 `compose.yaml` 不动**(本机本地构建路径不受影响)
- [x] **文档**:README 部署节改成「路径 1 拉镜像 / 路径 2 源码构建 / 路径 3 离线 tar」;
      《部署与运维》新增 §2.9 并在 §5.1 / §5.2 补拉取式升级与回滚(`session_secret` 随卷保留)
- [ ] **GHCR 包可见性(需用户操作)**:包默认私有,要么在包设置里改成 Public(NAS 免登录拉取),
      要么给目标机器配一个勾了 `read:packages` 的 PAT
- [ ] **NAS 真机首次部署(需用户操作)**:按《部署与运维》§2.9 走一遍,记录 `ps` / `/healthz` /
      `/calendar/login` / `/calendar` 404 纯文本的实测结果

## 本轮已完成(2026-10-12 第五轮:同步邮箱收敛到外部邮箱 + 只通知「看得见的人」)

> 文档编号对照:本仓库的《开发文档》与《验收测试记录》里没有单独的「界面可用性」轮,
> 所以同一件事在那里记作 **2026-10-12 第四轮**(开发文档 §18.15、验收记录 §3.8),
> 行文里出现的「第四轮」都是指本轮。

需求(真机反馈):共享日历里日程变动时,**范围内的家人收不到同步邮件**,仍只发给「设置」里
那几行同步邮箱。要求把设置里的同步邮箱功能**收敛到外部邮箱**,并让「某成员可见日历变动」时
把这次变更同步给**范围内用户已认证的邮箱**(附上未竟日程的 ICS 副本)。

- [x] **根因**:账号收件人虽然由 `users.email` 派生,却是一行**可被设置页删掉/停用**的普通记录
      ——`app/routers/pages.py` 的 `remove_recipient` / `toggle_recipient` **没有守卫**
      (只有 `/api/v1/sync-recipients` 拦了改名/删除);删掉之后又没有任何路径补回来
      (`ensure_family_setup` 的逐个 `sync_user_recipient` 不会置 `changed`,
      结尾的 `session.commit()` 因此不执行)。真机库里两位成员都已验证,只剩一行收件人。
- [x] **账号收件人改为派生 + 自愈**:新增
      `app/accounts.py::sync_account_recipients(session) -> int`(启用 + 已验证 → 一条**启用**的
      账号收件人;账号停用 → 该行 `enabled=False`,不删行;返回变化行数供调用方提交);
      `sync_user_recipient()` 补上「账号启用时把该行恢复成启用」
- [x] **三个对齐时机**:启动引导、**建发信任务之前**(`ensure_delivery_tasks`)、设置页渲染前
      ——不再依赖「上一次启动是否修好」
- [x] **引导修复必须落库**:`ensure_family_setup` 结尾改为**无条件 `session.commit()`**
      (逐项修复各自 `flush`,只看 `changed` 标记会漏判)
- [x] **设置页只维护外部邮箱**:`_recipients()` 只返回 `user_id IS NULL` 的行;
      新增只读的「账号邮箱(自动同步)」表(`_account_recipient_entries`);
      新增 `_guard_external_recipient`,页面上的停用/移除命中账号行 → **409**
- [x] **接口同步收口**:`PATCH /api/v1/sync-recipients {"enabled": …}` 命中账号行 → **409**
      (此前只拦改名/删除);要停止某个账号收信,唯一做法是**停用该账号**
- [x] **只通知看得见这次改动的人**:`publish()` 新增 `changed_calendar_ids`
      (不给时按 `event.calendar_id` 推断;PATCH 换日历传**新旧两条**);
      `ensure_delivery_tasks()` 新增 `_scope_calendar_ids()` 求交集,无交集不建任务;
      手动群发 / CLI 导出 / 后台修复不传 = 发给全部收件人
- [x] **顺带修掉一个真缺陷**:`PATCH /api/v1/events/{id}` 原先**先搬家再校验乐观锁**,
      而 `move_event_to_calendar` 会刷新 `updated_at` → 带 `expected_updated_at` 的换日历请求
      必然 **409**;改为先 `update_event` 再搬家
- [x] **测试**:新增 `tests/test_account_recipient_sync.py` **14 项**;适配既有用例
      (`test_mail.py::test_agent_submitted_offset_reaches_the_mail_body`、
      `test_mail_actor.py` 4 条改为把事件建在家庭共享日历里);
      全量 **450** 项通过(436 → 450),`ruff check app tests migrations` 干净
- [x] **文档**:AGENTS.md 不变量 17 改写、新增不变量 **24/25**;
      `docs/static/` 更新 `app-accounts`/`app-mailqueue`/`app-sync-service`/`app-bootstrap`/
      `app-models`/`app-routers-pages`/`app-routers-api-events`/`app-routers-api-agent`/
      `app-routers-api-recipients`/`templates`;README/开发文档(§6.5、§18.6、§18.7、新增 §18.15)/
      部署运维(§5 添加收件人、§6 权限表、§6.1 故障排查、§9.3 订阅与邮件范围)/
      验收记录(新增 §3.8)/todo.md 同步
- [x] **真机部署(2026-10-13 已完成)**:`docker compose up -d --build` 重建镜像并替换容器
      (此前跑的是 2026-10-12 01:33 的旧镜像,本轮改动都还没上机);部署前先在容器内
      `app.cli backup` 留了一份 `/data/backups/calendar-20261013-013350.db`;
      启动日志显示 `0007_email_verification -> 0008_delivery_actor` 迁移成功、无 `Traceback`、
      容器 `Up (healthy)`;`/healthz` 200、`/calendar/login` 200、`/calendar` 404 纯文本;
      **真机库 `sync_recipients` 从 1 行变 2 行**——那位成员的账号收件人被自动建出并提交;
      回滚事务探针确认:共享日历变动通知 2 人、管理员私人日历变动 0 人、手动群发 2 人(不写库不发信)
- [ ] **还没真的发过一封邮件**:以上都是「范围判定正确」的验证,实发链路(真 SMTP + 真收件箱)
      没触发。`.env` 里只有 `ADMIN_PASSWORD_HASH`,没法登录点「向当前人员发送最新版」;
      建议管理员在 `/admin/sync` 手动跑一次,或改一条共享日程后到 `/admin/sync` 看队列里
      是否出现两人各一条任务

## 上一轮已完成(2026-09-29)

- [x] 邮件队列(`/admin/sync`)显示**操作时间**(任务创建时刻)与**发送时间**,时间按日历默认时区展示
- [x] 日历设置(`/admin/settings`)的默认时区改为按地区分组的 30 项下拉框,含北京时间(序号 1)、
      悉尼时间(序号 11)、布里斯班时间(序号 12)等
- [x] `event` 接口(含 `/api/v1/events` 与 `/api/agent/events`)的 `timezone` 支持**序号索引**、
      中文名与下拉框整行文本;非法值报错提示可填序号
- [x] 时区清单可发现:`GET /api/v1/settings` → `timezone_choices`
- [x] 静态函数文档:`docs/static/`(新建目录与 8 份文档)
- [x] 测试:`tests/test_timezones.py` 46 项;全量 178 项通过,`ruff` 干净

## 上一轮已完成(2026-09-29:平台内管理远程接入令牌)

- [x] 设置页新增「远程接入令牌」卡片:生成 / 改名 / 停用 / 启用 / 撤销
- [x] 令牌**明文只在创建时显示一次**;数据库只存 `sha256(token)`,不进日志、不回显
- [x] 列表展示**指纹**(哈希前 16 位),与审计日志 `agent:<指纹>` 一一对应
- [x] `.env` 的 `AGENT_API_TOKEN` 保留为兜底,与库内令牌**取并集**鉴权
- [x] 迁移 `0003_agent_tokens`(唯一约束在 `token_hash`);撤销/停用后下一次请求即失效
- [x] 测试:`tests/test_agent_tokens.py` 13 项;全量 **191** 项通过,`ruff` 干净
- [x] 文档:`docs/static/app-token-store.md`(新增)等 4 份静态文档 + 部署/验收/README

## 本轮已完成(2026-09-30:提醒 VALARM + 备注链接)

- [x] 事件新增**提醒**:相对事件开始时间的多条提醒(`0` = 开始时,正数提前,负数延后,
      上限 30 天);全天事件以开始日 `00:00` 为基准(`1440` = 提前 1 天)
- [x] ICS 导出 `VALARM`(`ACTION:DISPLAY` + `DESCRIPTION:标题` + `TRIGGER:-PT15M` 这类相对时长)
- [x] 事件新增 **URL 链接**字段(备注里的链接),导出为 `VEVENT` 的 `URL`
- [x] 界面:表单提供 9 个常用提醒勾选项 + 自定义分钟输入框;日历 tooltip 显示提醒与链接
- [x] 接口:`/api/v1/events` 与 `/api/agent/events`(JSON 与表单形态)都接受 `reminders`/`url`
- [x] 迁移 `0004_event_reminders`(`events.reminders TEXT NULL`、`events.url VARCHAR(1000) NULL`)
- [x] **不变量守住**:入库前去重并从大到小排序(顺序不影响内容哈希);
      无提醒/无链接的事件导出逐字节不变(升级不触发重发)
- [x] 测试:`tests/test_reminders.py` 49 项;全量 **240** 项通过,`ruff` 干净
- [x] 文档:`docs/static/app-reminders.md`、`docs/static/app-ics.md`(新增)等 7 份静态文档
      + 开发文档/部署运维/验收记录/README

## 本轮已完成(2026-10-06:邮件附件只含尚未结束的日程 + 正文按提交时区)

- [x] **邮件附件不再含已结束的事件**:每个导出版本额外写一份 `calendar-v{N}-upcoming.ics`
      (仅「尚未结束」的事件),发信任务指向它
- [x] **分界是「此刻」而不是「今天 00:00」**:当天上午刚开完的会也不会跟着下一次邮件发出去;
      全天事件没有时刻可言,仍按当地日期判断(今天的保留)
- [x] **发布文件与版本快照保持完整**:订阅源 `calendar.ics` 仍含全部事件——手机上要看得到历史,
      而且「按挂钟时间过滤」若发生在导出层就会破坏不变量 3(无改动也递增版本 + 全员重发)
- [x] 判据复用 `expand_occurrences`:尚未结束的事件、**进行中**的事件、**起点很早但仍在重复**的序列都不会丢;
      `UNTIL` 已过期的重复序列才会被排除
- [x] 快照缺失(升级前的旧版本)时回退到版本快照,**任务不会因找不到文件而失败**
- [x] 正文提示语改为「尚未结束的日程……已经结束的日程不在附件里」;同步页二次确认文案同步更新
- [x] **邮件正文的「事件时间」按提交时区表述**:`start_at` 带 `+11:00` 就写 `14:00–15:00(UTC+11:00)`,
      事件的参照时区不变(订阅日历仍按收件人所在地显示);网页表单不带偏移量,行为不变
- [x] 无数据库迁移(复用 `snapshot_path`/`attachment_hash`);不变量 3/6 复核通过
- [x] 测试:`tests/test_mail.py` 新增 10 项(含 1 项判别性用例:旧口径下会失败);全量 **250** 项通过,`ruff` 干净
- [x] 部署:两次容器重建(`Up (healthy)`);真实链路已在 v158 上验证(用户经快捷指令新建日程 →
      附件为 `calendar-v000158-upcoming.ics`,两位收件人各收到一封),收紧口径后探针确认当天已结束的
      日程也被排除
- [x] 文档:新增 4 份静态函数文档(`app-exporter`/`app-mailqueue`/`app-sync-service`/`app-timeutil`)
      + 开发文档/部署运维/验收记录/README/AGENTS.md

## 本轮已完成(2026-10-08:家庭多人共享日历)

需求:把单用户服务改造成「以家庭为单位」的多人共享日历——3–5 个账号登录、每人独立的私人日历、
外加家庭共享日历。

- [x] **账号体系**:`users` 表(用户名/显示名/邮箱/颜色/时区/角色/启用/强制改口令);
      `admin`(家长)与 `member` 两种角色;管理员可停用、重置口令(一次性临时口令)、改角色、删除;
      不能停用/删除自己,也不能删掉最后一个管理员(409)
- [x] **日历与权限**:`calendars`(personal/shared)+ `calendar_members`(owner/editor/viewer);
      **私人日历只有本人可见可写,管理员也读不到(读 404、写 403)**;共享日历全员可见、
      只有 `owner`/`editor` 能写,新成员默认 `viewer`;只有管理员能建共享日历与调权限
- [x] **邀请码注册**:`invite_codes` 只存哈希、次数上限(默认 1,上限 20)与有效期;
      `/register?code=...` 自助注册并自动登录(限流 5 次/10 分钟);明文只在创建时显示一次
- [x] **每人一条订阅地址**:`feed_tokens` 只存哈希;`/calendar/<令牌>.ics` 服务**该账号可见范围**
      的合并结果;家庭令牌(`user_id` 为空)只服务共享日历;撤销后地址立刻 404
- [x] **旧地址兼容**(2026-10-08 的做法;**2026-10-12 已取消,见本轮已完成记录**):`/calendar` 与 `/calendar.ics` 当时继续可用,绑定**首位管理员**的可见范围
      (不是全库并集),无首位管理员时 503
- [x] **邮件按收件人范围出附件**:账号收件人收到自己可见日历的快照,外部邮箱只收到家庭共享日历;
      正文不再出现并集订阅地址(当时仅首位管理员沿用旧 `SUBSCRIPTION_URL`;**2026-10-12 起改为账号收件人收到自己的固定地址、外部邮箱一条都不给**)
- [x] **Agent 令牌绑定账号与日历**:每枚令牌带 `user_id`/`calendar_id`,按该账号权限落库;
      跨账号复用同一个 `client_ref` 返回 409 且不泄露事件内容,跨账号读 404
- [x] **多目标导出**:`export/feeds/<key>.ics` 对外发布,`snapshots/calendar-v{N}-<key>.ics` 与
      `-<key>-upcoming.ics` 供邮件;**版本号与 `content_hash` 仍由全库并集决定**(内部产物
      `export/calendar.ics`),任何成员的改动都会推进全局版本
- [x] **界面**:按角色分级的导航;日历页多日历侧栏勾选与着色、只读日历体裁不可编辑;
      事件表单带日历下拉与只读提示;「我的设置」(资料/口令/订阅)、「成员」(账号/邀请码/共享日历权限)、
      设置页(收件人范围/家庭订阅地址/令牌归属)全部无内联脚本
- [x] **迁移与引导**:迁移 `0005_family_accounts` 只做结构;启动引导 `ensure_family_setup` 幂等补齐
      首位管理员、私人日历、家庭共享日历、成员关系与历史数据归属(原先的日程全部归入首位管理员的
      私人日历,旧订阅与旧收件人行为不变)
- [x] **修掉一个真实缺陷**:`build_range` 缺省起点原先用 UTC 的「今天」套当地零点,UTC+10 时区在
      当地刚过午夜时会把「昨天」的全天事件重新放进邮件附件;改为按应用默认时区的当地今天计算
- [x] **删账号不留悬空归属**:SQLite `batch_alter_table` 加列不带外键,升级库里没有
      `ON DELETE SET NULL`;`delete_user` 改为**显式**删除个人日历与其中事件、并把剩余事件的
      `created_by_user_id` 置空(两条建库路径行为一致),并写进 AGENTS.md 不变量 20
- [x] 测试:新增 `tests/test_family_accounts.py`(35)、`tests/test_family_web.py`(34)、
      `tests/test_family_pages.py`(50)、`tests/test_family_mail.py`(19);全量 **388** 项通过,
      `ruff check app tests` 干净。补的 6 项针对文档审计发现的缺口:注册限流 429、临时口令改密引导、
      注册页 CSP、403 的页面/JSON 两种形态、目标订阅文件跨挂钟确定性、`build_range` 当地今天回归
      (后者已确认:退回旧口径时用例会失败)
- [x] 文档:静态函数文档新增 `app-models`/`app-accounts`/`app-invites`/`app-feed-tokens`/
      `app-bootstrap`/`app-security`/`app-config`/`app-main`/`app-routers-{auth,feed,api-events,api-agent,api-recipients}`
      与 `migrations-0005`,并更新受影响的既有文档;README/AGENTS.md/开发文档/部署运维/验收记录同步

## 本轮已完成(2026-10-11:家庭版真机部署 + 收件人范围缺陷修复)

需求:把家庭版真正部署到 Docker 上,并让家人能用起来。

- [x] **升级前一律先备份**:容器内 `python -m app.cli backup` 生成
      `data/backups/calendar-20261011-135103.db`,另把 `calendar.db`/`admin.hash`/`session_secret`
      复制留档到 `data/backups/pre-family-20261012-005103/`
- [x] **真机升级等价性实测**:`0004_event_reminders -> 0005_family_accounts` 自动迁移并建出首位
      管理员;82 条事件全部回填(`calendar_id IS NULL` 计数 0)、新建私人日历与「家庭共享日历」;
      `app.cli verify` 一致(版本 159);旧 `/calendar` 当时仍 200 且仍是首位管理员的可见范围
      (手机上的订阅地址**当时**不用改;**2026-10-12 起必须换成各人的固定地址**);
      `ADMIN_PASSWORD_HASH` 沿用旧哈希,没有强制改密
- [x] **成员面按精确路径暴露给 tailnet**(2026-10-11 的决定;**2026-10-12 已收窄为只挂 `/calendar` 一段**):
      当时补挂 `/login`、`/logout`、`/register`、`/admin`、`/api`、`/static`,既有四个挂载未动。
      原因是成员必须能自己登录/注册/生成只属于自己的订阅地址(订阅令牌一律绑定当前登录者,
      管理员不能代生成);`/admin/*` 里的管理接口对成员仍 403
- [x] **修掉一个只有真机能发现的缺陷**:首次引导把**每个**历史收件人都写成
      `user_id = 首位管理员`,同一账号名下出现多行;第二次启动起
      `sync_user_recipient()` 的 `scalar_one_or_none()` 抛 `MultipleResultsFound`,
      容器进入重启循环(`exit=3`)。测试库走 `create_all`、回归用例只放了**一个**历史收件人,
      所以自动化没拦住
- [x] **修复方式:身份与范围拆开**。迁移 `0006_recipient_scope` 新增
      `sync_recipients.scope_user_id`(范围)并把 `user_id` 收窄为「账号收件人」的身份标记
      (每账号一行);迁移同时修复已有重复绑定(保留邮箱与 `users.email` 一致的那行,其余降级)。
      代码侧:`recipient_scope_user_id()` = `scope_user_id or user_id or None`;
      `sync_user_recipient()` 不再用 `scalar_one_or_none`(重复时告警 + 按邮箱匹配 + 认不出不删);
      `delete_user` 把挂在被删账号上的范围退回家庭共享;邮件附件目标键与收件人范围标签同步
- [x] **修复后真机验证**:迁移成功;两个历史收件人现在 `user_id = NULL`、
      `scope_user_id = 首位管理员`(收信范围与升级前一致),`user_id` 重复行 0;
      **连续重启两次均 `Up (healthy)` 且 `RestartCount=0`**;`verify` 一致;
      `/admin/partials/recipients` 200 且显示「沿用 admin 的可见日历」,
      `/api/v1/sync-recipients` 返回 `scope: member`;对外 `/calendar` 当时仍 200(**该地址 2026-10-12 已失效**)
- [x] **会话密钥加固**:`.env` 里的 `SESSION_SECRET` 只有 5 个字符,一直盖住
      `data/session_secret` 的 64 字符随机密钥;界面开放后改为留空走文件密钥
      (副作用:已登录的浏览器要重新登录一次;当时订阅地址是随机令牌所以不受影响)。
      **注意 2026-10-12 起这条副作用变了**:固定订阅地址与验证码哈希都由该密钥派生,
      再动这个文件会让所有人的订阅地址失效(见待办里的运维文档指向)
- [x] **新增回归测试** `tests/test_family_upgrade_recipients.py` 4 项:两个历史收件人反复引导不崩
      且范围正确、被旧引导写坏的重复绑定也能起来、迁移修复函数两分支(有/无账号邮箱);
      并把 `test_legacy_recipients_keep_owner_scope_on_first_upgrade` 改为断言 `scope_user_id`。
      另给 `tests/test_family_mail.py` 补 1 项(`test_legacy_external_recipient_keeps_owner_scope_attachment`:历史外部邮箱的范围是被绑定账号的,但正文不能写成「你可见的日历」、也不能给它旧订阅地址)。全量 **393** 项通过,`ruff check app tests` 干净
- [x] 文档:AGENTS.md 不变量 16 改写、新增不变量 21 与「部署时的三个陷阱」;
      README 的注册地址改为 `/register?code=...`、部署节改写暴露面;`.env.example` 注释;
      静态函数文档与开发/部署/验收文档同步(见本轮提交)

## 本轮已完成(2026-10-12:公开面前缀 + 固定订阅地址 + 邮箱验证)

需求:把公开面收进一个前缀(外网只挂 `/calendar`)、给每人一条**永不变更**的固定订阅地址、
并给账号邮箱加一道**验证关卡**。

- [x] **公开面统一到 `/calendar` 前缀**:新增配置 `PUBLIC_BASE_URL`(默认
      `https://sisi-laptop.tail921448.ts.net/calendar`)**取代** `SUBSCRIPTION_URL`,它的 path 部分就是
      挂载前缀;成员入口变成 `/calendar/register`、`/calendar/login`、`/calendar/admin/...`、
      `/calendar/verify-email`、`/calendar/<令牌>.ics`
- [x] **新增 `app/urls.py`**:请求开始时把前缀绑进 `ContextVar`(模板全局函数 `bp()`、Python 侧 `url()`),
      页面里的链接**跟着当次请求的前缀**渲染;绝对地址(邀请链接、订阅地址、邮件正文)一律取自
      `PUBLIC_BASE_URL`。本机裸路径(`http://localhost:443/admin`)仍然可用
- [x] **对外只挂一段**:Tailscale Serve 实测只剩 `/`、`/dsh`、`/agent`、`/calendar`;裸 `/login`、
      `/admin`、`/api`、`/static` 已撤掉(经 tailnet 落到 DSH 上返回 401)
- [x] **旧的无令牌订阅地址彻底取消**:`/calendar`、`/calendar/`、`/calendar.ics` 一律
      `404 text/plain`(正文「此地址不再提供日历…」),对浏览器 303 → `/calendar/admin`;
      `app/routers/feed.py` 只剩一条 `/{token}.ics`
- [x] **每个账号一条固定订阅地址**:`sisi_feed_ + HMAC(session_secret, "feed:<user_id>")[:40]`,
      服务端派生、永不变更;账号创建时(含引导幂等回填)登记 `feed_tokens` 行
      (标签「账号订阅地址(固定)」,库里只存 sha256);界面**不给停用 / 撤销(409)**,但可以随时
      重新显示、重新发信
- [x] **邀请链接是可直接复制粘贴的完整地址**:`https://…/calendar/register?code=…`
- [x] **邮箱验证**:注册**必须填邮箱**;6 位验证码、30 分钟有效、最多错 5 次(第 5 次直接作废)、
      重发冷却 60 秒;未验证的成员页面 303 到 `/calendar/verify-email`、`/api` 403
      `email_not_verified`,也不能成为邮件收件人;**首位管理员豁免**;改邮箱立刻打回待验证并重发;
      库里只存验证码的 HMAC,验证码与地址明文只出现在邮件正文里
- [x] **验证通过后补发一封「你的固定订阅地址」邮件**;日历邮件正文也按收件人身份区分:账号收件人
      拿到**他自己**那条地址,外部邮箱(含历史邮箱)一条都不给
- [x] **迁移 `0007_email_verification`**:`users` 新增 5 列,并把已有非空邮箱回填
      `email_verified_at = created_at`(否则升级完成后老成员会被验证页挡住)
- [x] **CLI 安全网**:`app/cli.py::_warn_if_container_path()` —— 仓库里有 `./data/calendar.db` 而本次
      `DATA_DIR` 指向别处时打印「本次操作的数据库是 …」(背景:`.env` 的 `DATA_DIR=/data` 在 Windows
      宿主机上会被解析成 `D:\data`,宿主机跑 CLI 会悄悄操作另一个库)
- [x] **测试**:新增 `tests/test_public_prefix.py`(9)、`tests/test_email_verify.py`(12)、
      `tests/test_cli.py`(3);`test_family_accounts.py` 35→36、`test_family_web.py` 34→35、
      `test_family_pages.py` 50→53。全量 **422** 项通过,`ruff check app tests` 干净
- [x] **真机部署实测(2026-10-12)**:重建镜像后容器自动从 `0006_recipient_scope` 升到
      `0007_email_verification`,`Up (healthy)`、`RestartCount=0`;`/calendar/login` 与
      `/calendar/register` 200、`/calendar/static/css/app.css` 200;`/calendar`、`/calendar/`、
      `/calendar.ics` 均 404 纯文本;两个账号的固定地址各自 `200 text/calendar`(管理员 7 条未结束
      日程,成员思思 0 条)
- [x] **文档**:AGENTS.md 不变量 8/14/16/18/22/23 与「部署时的三个陷阱」;README 的地址布局与
      邮箱验证规则;验收记录新增 §3.5(含 9 个前缀用例、12 个邮箱验证用例逐一列出、迁移盲区);
      `docs/static/` 增补 `app-accounts`/`app-email-verify`/`app-feed-tokens`/`app-invites`/`app-urls` 等

## 本轮已完成(2026-10-12 第三轮:界面可用性 / 真机 UI 反馈)

真机试用反馈:订阅地址想要复制按钮、「新建日程」那块太丑,并追问快捷指令的地址与日历归属。

- [x] **订阅地址一键复制**:`partials/feeds.html`(固定地址 + 临时明文)、
      `partials/family_feeds.html`(临时明文)、`partials/members.html`(邀请链接)各加「复制」按钮;
      新增 `static/js/copy.js`(事件委托 + `navigator.clipboard` + `execCommand` 兜底 +
      点只读输入框自动全选 + 「已复制/复制失败」1.6 秒反馈),由 `base.html` 用 `asset()` 加载
- [x] **侧栏排版重做**:`.sidebar-cta`(留白 + 内联 SVG「+」图标)、`.sidebar-label` 小标题、
      带边框圆角的 `.calendar-list`(行间细线)、底部提示缩短为一句;用无头 Chrome 在**真实 250px 栏宽**
      下逐版截图核对过观感
- [x] **修掉「页面错版」的根因**:静态资源加内容指纹 —— 新增
      `app/urls.py::asset()/asset_version()` 并注册为 Jinja 全局 `asset`,六个模板的 CSS/JS 全部改用
      `/static/...?v=<大小>-<mtime>`(浏览器长期缓存旧 CSS 正是真机错版的原因);
      `templates.md` 里补了「模板不得再写 `{{ bp() }}/static/...`」的不变量
- [x] **表格窄列不再折行**:`.table th { white-space: nowrap }`、
      `.table td.row-actions { white-space: nowrap }`;固定行说明缩短为「不可撤销」(理由进 `title`)
- [x] **测试**:`tests/test_family_pages.py` 53→57(`test_sidebar_primary_action_is_a_proper_button`、
      `test_static_assets_carry_a_version_stamp`、`test_feed_cards_offer_a_copy_button`、
      `test_new_token_reveals_offer_a_copy_button`);全量 **426** 通过,`ruff` 干净
- [x] **文档**:`docs/static/templates.md`(资源指纹、侧栏重做、复制按钮、新增 `static/js/copy.js` 一节)、
      `docs/static/app-urls.md`(`asset()` 契约)、`docs/static/app-deps.md`(`asset` 全局)、
      验收记录新增 §3.6(含快捷指令接口的答复)
- [ ] **顺手清理候选(本轮未动)**:仓库里有历史提交进来的 `tmp-verify/`(部署验证残留,
      含 `cookies.txt` 会话 cookie 与几个临时 HTML/JSON),建议后续删掉并加进 `.gitignore`

### 快捷指令(本轮只答复,未改行为)

- **URL 没变**:仍是 `https://sisi-laptop.tail921448.ts.net/agent/events`(线上实测无令牌 POST → 401、
  GET → 405);`/calendar/api/agent/events` 与裸 `/api/agent/events` 同样可用
- **日历归属优先级**:令牌的 `calendar_id` → 请求体的 `calendar_id` → 该账号的默认可写日历
  (`app/routers/api_agent.py::_token_calendar_id`)。所以「跟随令牌」只在**平台内令牌**上成立
- **本机现状**:`agent_tokens` 表为空,快捷指令用的是 `.env` 的 `AGENT_API_TOKEN`,而环境变量令牌
  **不绑定日历**,所以请求体不带 `calendar_id` 时日程落进**首位管理员的个人日历**;
  想让快捷指令直接写进「家庭共享日历」,建一枚平台令牌并把「默认日历」选成它即可

## 本轮已完成(2026-10-12 第四轮:同步邮件标明「发起人」)

真机反馈:同步邮件只说「本次操作:新增日程」,**收信的家人看不出是谁改的**。

- [x] **邮件正文新增「发起人」一行**:`app/mailqueue.py::_body_for` 在「本次操作:…」
      之后写 `发起人:<账号显示名>`(显示名优先,没有显示名用用户名)
- [x] **只标具体日程**:只对「新增/修改/删除日程」写这一行;**手动同步不写**
      (「重新生成 ICS」「向当前人员发送最新版」是管理员主动群发),也**从不**凭空写「未记录」
- [x] **语义定死:发起人是「谁在操作」,不是「事件是谁建的」**。
      `app/sync_service.py::publish` 新增 `actor_user_id` / `actor_label` 并**不读**
      `event.created_by_user_id`(那会把别人的改动记到创建人头上);五个调用点各自显式传入:
      `api_events` ×3 = 当前登录者、`api_agent` = 令牌绑定账号、`api_sync`/`pages` 手动动作 =
      当前管理员;CLI 与后台 `repair_if_needed` 不传(正文写「未记录」)
- [x] **名字取值**:新增 `app/mailqueue.py::_actor_name` —— `actor_user_id` **现查**账号
      (改名立刻生效)→ 查不到退回 `actor_label` 快照 → 都没有写「未记录」
- [x] **发起人在建任务那一刻固化**:同版本重试沿用任务记录,不会改成「现在登录的人」
- [x] **迁移 `0008_delivery_actor`**:`mail_deliveries` 加 `actor_user_id`(无外键)与
      `actor_label`(显示名快照);**只加列、不回填**,升级前的历史任务显示「未记录」
- [x] **删账号不留悬空引用**:`app/accounts.py::delete_user` 显式把 `actor_user_id` 置空、
      **保留 `actor_label` 快照**(不变量 20 的同一理由:升级库的加列没有外键约束)
- [x] **管理面可见**:`/admin/sync` 队列表格在「类型」后新增「发起人」列(历史任务显示 `—`,
      手动同步也记录操作者备查);`GET /api/v1/mail-deliveries` 每项新增 `actor_label`
      (`app/schemas.py::DeliveryRead`)
- [x] **测试**:新增 `tests/test_mail_actor.py` **10 项**(覆盖增删改三种操作、成员改管理员的
      事件时发起人是成员、Agent 令牌、手动同步不写、历史任务「未记录」、删账号后仍说得清、
      改名立刻生效、队列页面与 API 可见、历史任务渲染不出现 `None`);
      全量 **436** 项通过(426 → 436),`ruff check app tests migrations` 干净
- [x] **文档**:`docs/static/` 更新 `app-mailqueue`/`app-sync-service`/`app-routers-api-settings`/
      `templates`/`app-accounts`,新增 `migrations-0008`;开发文档/部署运维/验收记录(§3.7)/README 同步
- [x] **真机部署与验证(2026-10-13 已完成)**:随第五轮的镜像重建一起上机
      (`docker compose up -d --build`,镜像构建 01:33:59Z、容器启动 01:34:01Z,见本文件顶部第五轮
      的「真机部署」条);容器内 69 个运行时文件与工作区**逐字节一致**,
      `alembic_version = 0008_delivery_actor`,队列表格已带「发起人」列。
      **端到端**:用户以思思在 01:42:29 新建日程「吃饭」→ 两条任务的 `actor_label` 都是「思思」,
      两封邮件随后 `status=sent`(两位家人各一封)。另在生产库**副本**上用已部署代码跑了 12 项
      检查(主路径 / 手动同步不写 / 删账号靠快照 / 历史任务)全部通过,真实库未被改动。
      详见 `docs/验收测试记录.md` §3.7
- [ ] **真机复现差异点(未做)**:上面验证的是「账号本人建日程」;「成员改**管理员建的**日程、
      邮件写改动的那个成员」这条差异点仍只有自动化覆盖
      (`test_mail_actor.py::test_member_edit_mail_names_the_member_display_name`),未在真机人工复现

## 待办

### 家庭版遗留(按需再做)

- [x] **在每台 iPhone 上把订阅地址换成新的固定地址**(2026-10-12 完成):
      上一代仅两位测试用户,已全部人工换成 `/calendar/<自己的令牌>.ics`;
      旧的无令牌 `/calendar` 不做兼容,后续只需确认固定地址继续可用
- [ ] **共享日历精确到事件级别的编辑权限**:当前 `editor` 能改共享日历里的**任何**事件;
      如果需要「只能改自己创建的事件」,要在 `CalendarAccess.can_write` 之外再加一层
      `created_by_user_id` 判定,并同步改 `/api/v1/events` 与页面表单
- [ ] **成员之间的私下共享**:现在是「私人 + 一条全家共享」两档。若以后需要「只和爸爸共享」,
      可复用 `calendar_members` 直接把某人的私人日历加第二个成员,但必须先想清楚
      「管理员能不能读」这条边界(当前答案是不能,加成员等于成员自己授权)
- [x] **真机确认订阅**(2026-10-11 完成,2026-10-12 换成固定地址后需再确认一次,见上一条)
- [ ] **邮件范围真机确认**(2026-10-12 第四轮已把机制改成自动派生 + 按可见范围筛选,
      待真机复核):成员邮箱验证通过后**不需要**在设置页添加,确认他只收到自己可见范围的附件、
      且正文里的订阅地址是**他自己**那条;给外部邮箱(长辈)发布一次,确认附件里没有任何人的
      私人日程、正文里一条 `sisi_feed_` 地址都没有;再确认改动落在管理员私人日历时**不会**
      打扰外部邮箱
- [ ] **邀请码闭环实测**:管理员发码 → 成员注册(**填邮箱 + 输验证码**)→ 家长提升为 `editor` →
      他改共享日历 → 确认其他成员收到的是更新后的订阅与邮件
- [ ] **历史外部收件人的归宿**:单用户版带上来的两个邮箱现在靠 `scope_user_id` 沿用首位管理员的
      可见范围(行为与升级前一致),但它们会一直收到**家长的全部私人日程**(现在**不会**再收到任何
      订阅地址)。家人各自注册账号后,到「设置 → 邮件收件人」里把这两行移除或改成普通外部邮箱
      (只收家庭共享日历)
- [x] **`data/session_secret` 轮换的影响写进运维文档**(2026-10-12 完成):轮换该密钥会让
      **所有固定订阅地址与所有验证码哈希一起失效**,需要让每人重新复制一次「我的设置」里的地址;
      《部署与运维》已同步说明必须备份/恢复原密钥,不得把轮换当成日常升级步骤
- [ ] **账号删除的数据归宿再确认**:删除账号会连同他的私人日历与其中的事件一起删除(共享日历里
      的事件保留,`created_by_user_id` 由 `delete_user` **显式**置空——升级库里没有外键约束,
      不能依赖 `ON DELETE SET NULL`)。若希望先归档再删,需要新增「导出该账号私人日历」入口
- [ ] **升级演练**:在一份真实 `data/` 副本上跑 `alembic upgrade head` + 启动引导,确认原有日程全部
      落在首位管理员的私人日历里(注意 `uv run alembic` 固定打真实库那条约束)
      → 2026-10-11 已在真实 `data/` 上完成一次(先备份),2026-10-12 又完成了一次 `0006 → 0007`
      的真机升级,见本轮已完成记录
- [ ] **迁移的自动化盲区**:`migrations/versions/0005`–`0007` 的 `upgrade()` / `downgrade()` 与
      `0007` 的邮箱回填**没有测试覆盖**(测试库走 `create_all`,不执行迁移脚本),目前只靠真机升级
      验证。要补得在测试里单独调 alembic 的 `op`,或准备一份旧 schema 的临时库

### 本轮遗留(按需再做)

- [ ] **邮件附件过滤的可选开关**:当前是固定行为。若以后想要"回退成完整日历",恢复
      `app/exporter.py::_build_mail_attachment` 的使用一处即可,也可加一个环境变量开关
- [ ] **只改了已结束事件时的空附件体验**:目前附件是一份空日历(正文仍写明本次变更的事件);
      若实际使用中觉得别扭,再考虑"至少保留本次变更的事件"
- [ ] **真机确认**:用快捷指令带 `+11:00` 建一条日程,核对邮件正文写的是你输入的钟点、
      附件里没有已结束的日程、订阅日历仍按当地显示

### 提醒相关的后续(按需再做,先看真实使用)

- [ ] **iPhone 真机确认**:订阅日历的提醒由本地「默认提醒时间」决定,系统不保证逐条响应
      `VALARM`;需要在真机上确认「订阅只读日历」与「导入 ICS 到系统日历」两条路径的实际表现
- [ ] 评估是否需要**全局默认提醒**(新事件自动带上某组提醒):需要动 `app_settings`
      与导出默认值,先确认真的会用再开
- [ ] 评估是否需要 `TRIGGER;RELATED=END`(相对结束时间的提醒)与 `REPEAT`/`DURATION`(贪睡)

### 高优先级(**仍是最高优先级**:上一次事故暴露)

- [ ] **自动备份**:`data/backups/` 在那次事故时是**空的**——备份只有手动 `python -m app.cli backup`
      这一条路径,没有任何调度。建议在 `scheduler.py` 里加每日备份 + 保留裁剪,或在
      `compose.yaml` 之外用主机计划任务调用 `app.cli backup`。
      事故详情与恢复过程见 `docs/验收测试记录.md` 的「同轮事故与恢复记录」。
- [ ] **迁移的沙箱**:`uv run alembic` 固定打 `./data/calendar.db`,需要一个「在副本上试迁移」的
      正规做法(临时 ini 或加 `--sql` 干跑),避免下次再误伤真实库。

### 需要真实环境(累计未做)

- [ ] **令牌在真机上的闭环**:在设置页生成一枚令牌 → 填进 iOS 快捷指令 → 建一条日程 → 撤销该令牌 → 确认再调用返回 401
- [ ] **iPhone 真机订阅**:按《部署与运维》3.3 添加订阅,覆盖普通/全天/跨日/重复事件;
  **2026-10-12 起必须用各人自己的固定地址** `/calendar/<自己的令牌>.ics`(旧的 `/calendar` 已失效)
- [ ] **iOS 快捷指令实测**:用 `timezone=1` 这条新路径建一条日程,确认落库时区为 `Asia/Shanghai`
- [ ] **容器内页面复核**:邮件队列表格由 8 列增至 **11** 列(2026-10-12 第三轮又加了「发起人」),
  确认窄屏下的横向滚动是否可接受
- [ ] **真实 SMTP 发信**:配置真实邮箱后点「向当前人员发送最新版」,核对队列里的操作/发送时间
  与「发起人」列(手动同步那一行显示操作者,但邮件正文里**不**出现发起人一行)
- [ ] 主机重启后确认 `docker compose ps` 仍为 `Up (healthy)`
- [ ] 从第二台设备实测 443 端口不可达

### 值得做但未排期

- [ ] 邮件队列增加筛选(按状态/收件人)与分页,当前固定最近 30 条
- [ ] 队列时间列支持点击切换「本地时间 / UTC」显示
- [ ] 时区清单支持在设置页自定义常用项(目前只能改环境变量里的 IANA 名)
- [ ] `timezone_choices` 做成公开只读接口,方便快捷指令直接拉取序号表
- [ ] 令牌增加「最近使用时间」列(可从审计日志聚合,不必每请求写库)
- [ ] 令牌支持可选的过期时间 / 使用次数上限

## 约束提醒

- **写进文档的真机证据必须脱敏**(2026-10-14 的教训):日志有 `app/util.mask_email`、库有 SQLite
  兜底,而 `docs/`、`README.md`、`AGENTS.md`、`todo.md` 是**纯文本,只有约定保护**。从生产库
  粘贴证据时,邮箱写成 `9******@qq.com` 这种掩码,口令、令牌、邀请码、验证码、固定订阅地址
  只写指纹或掩码,不写明文;指代某人用显示名或用户名。第五轮的验收记录曾把 `sync_recipients`
  的真实邮箱明文写进 `docs/验收测试记录.md` 并推送过一次(2026-10-14 已打码,但历史里仍在)。
- **隐私边界是硬约束**:私人日历只有归属账号可读可写,管理员也不行(读 404、写 403);
  新增任何读接口都要经过 `CalendarAccess.viewable_ids()`,写接口经过 `can_write()`。
  导出与邮件同样按目标范围裁剪,越界数据不得出现在任何产物里。有测试守着
  (`tests/test_family_accounts.py`、`tests/test_family_web.py`)。
- **订阅范围**:`/calendar/<令牌>.ics` 只服务该令牌账号的可见范围;**旧的无令牌 `/calendar`、
  `/calendar/` 与 `/calendar.ics` 已经永久取消**,一律 `404 text/plain`,绝不能退化成全库并集,
  也不能再退回「首位管理员的可见范围」(并集只是内部一致性与版本判定的产物)。
- **公开面的前缀只有一段**:所有成员入口与订阅地址都在 `PUBLIC_BASE_URL` 的 path 前缀下
  (默认 `/calendar`),应用内链接按**当次请求**的前缀渲染(`app/urls.py` 的 `ContextVar`,
  模板用 `bp()`、Python 用 `url()`),绝对地址一律取自 `PUBLIC_BASE_URL`;本机裸路径仍然可用。
  serve 上只挂 `/calendar`(**不要再挂裸 `/login`、`/admin`、`/api`、`/static`**),
  且挂载路径必须与后端路径**逐字一致**(本仓库的用法不剥前缀)。
- **每个人只有一条固定订阅地址**,由服务端密钥派生(`HMAC(session_secret, "feed:<user_id>")`),
  永不变更、界面不给停用/撤销(409);库里仍只存 `sha256`,明文只允许出现在界面展示与本人邮件里。
  **轮换 `data/session_secret` 会让所有固定地址与所有验证码哈希一起失效**(见待办里的运维文档指向)。
- **邮箱必须验证过才算数**:非管理员「有邮箱但未验证」进不了页面与接口(页面 303 到
  `/calendar/verify-email`、`/api` 403 `email_not_verified`),也不能成为邮件收件人;首位管理员豁免;
  验证码 6 位 / 30 分钟 / 最多 5 次 / 重发冷却 60 秒;改邮箱立刻打回待验证。验证码与地址明文
  不进日志,库里只有 HMAC 哈希(AGENTS.md 不变量 22)。
- **邮件范围按收件人区分,而且**账号收件人是派生数据(2026-10-12 第四轮,AGENTS.md 不变量 24/25):
  `sync_recipients.user_id` 非空的行完全由 `users.email` + `users.enabled` + `email_verified_at`
  决定,由 `app/accounts.py::sync_account_recipients()` 在**启动引导、建发信任务之前、设置页
  渲染前**三个时机对齐(启用且已验证 → 一条启用的账号收件人;账号停用 → 该行停用但不删)。
  设置页与 `/api/v1/sync-recipients` **只维护外部邮箱**,命中账号行的改名/启停/删除一律 409;
  要停收信只能停用账号。**一次改动只通知「可见范围包含这条日历」的收件人**
  (`publish(changed_calendar_ids=…)` → `_scope_calendar_ids()` 求交集;换日历传新旧两条;
  手动群发/CLI/后台修复不传 = 全员)。账号收件人 = 本人可见日历 + **他自己**那条固定地址,
  外部收件人 = 家庭共享日历且**一条订阅地址都不给**;并集地址与 `SUBSCRIPTION_URL` 已彻底取消。
- **收件人的身份与范围必须分开**:`sync_recipients.user_id` 是「这行属于哪个账号」的身份标记
  (**每个账号最多一行**),`scope_user_id` 才是收信范围;引导/迁移/`sync_user_recipient` 都不得把
  多个邮箱写进同一个 `user_id`(真机升级时正是这个写法让第二次启动必崩,迁移 `0006_recipient_scope`
  修复)。见 AGENTS.md 不变量 21 与 `tests/test_family_upgrade_recipients.py`。
- **邀请码与订阅令牌只存 `sha256`**:明文只允许出现在创建成功那一次响应里,列表只显示指纹。
- **引导必须幂等**:`ensure_family_setup` 在已有家庭数据时不得有任何副作用;结构变更只写迁移。
- **删除账号必须显式清空归属**:SQLite `batch_alter_table` 加列不带外键,升级库的
  `events.created_by_user_id` 等列没有 `ON DELETE SET NULL`,`delete_user` 必须在应用层
  删个人日历/事件并把剩余事件创建人置空(AGENTS.md 不变量 20)。
- **订阅文件的确定性**:`export/feeds/<目标键>.ics` 与并集一样只能取决于数据库状态,
  跨挂钟导出的字节必须一致(有 `test_target_files_are_deterministic_across_wall_clock` 守着);
  缺省日期窗口的起点取**应用默认时区的当地今天**,不是 UTC 的今天。
- **按挂钟时间过滤只允许发生在邮件专用快照上**(`snapshots/calendar-v{N}-<目标键>-upcoming.ics`,
  历史版本是 `calendar-v{N}-upcoming.ics`)。发布文件 `export/feeds/<目标键>.ics`、版本快照
  `calendar-v{N}.ics` 与 `content_hash` 必须保持完整,否则不变量 3 立刻破:没有改动的保存也会因
  某条日程刚好过期而改变哈希、递增版本、向所有人重发。有测试守着
  (`test_attachment_drops_past_events_but_feed_keeps_them`)。
- **日期范围的缺省起点按应用默认时区的「今天」算**,不要图省事用 `utcnow().date()`
  (UTC+10 这类时区会因此把昨天的全天事件算进今天,已修过一次)。
- 判断"有没有给偏移量"一律用 `is None`:**`timedelta(0)`(`Z` 提交)是假值**,
  用真值判断会把 UTC 误判成"没给",见 `app/sync_service.py::input_time_text`。
- `TIMEZONE_CHOICES` 的序号是**对外契约**:只能追加到表尾,不得重排或改号。
- 邮件队列表格新增列不需要数据库迁移——`created_at` / `sent_at` 早已存在。
- 时间一律以 naive UTC 落库,展示层换算统一走 `localtime` 过滤器。
- **令牌明文只允许出现在创建成功那一次响应里**(远程接入令牌、邀请码、订阅令牌、管理员重置出的
  临时口令都一样);库里只存 `sha256`(口令存 Argon2id 哈希),日志里只允许出现指纹。
  改令牌相关代码时先确认这两条没被破坏(有测试守着)。
- **成员界面从 2026-10-12 起统一走 `/calendar` 前缀**:成员要能自助注册、登录、生成自己的订阅
  地址,所以这段前缀承载了成员面(原来 2026-10-11 挂的裸 `/login`、`/logout`、`/register`、
  `/admin`、`/api`、`/static` 已撤掉)。权限边界是账号 + 角色,不是网络隔离;
  改完要确认 `/calendar/login`、`/calendar/register` 200,`/calendar`、`/calendar.ics` 仍是
  404 纯文本。会话密钥请留空走 `${DATA_DIR}/session_secret`,不要在 `.env` 里填短口令
  (它盖住文件里的强密钥);也**不要**顺手轮换这个文件,它同时是固定订阅地址与验证码哈希的密钥。
- **`uv run alembic` 不读 `DATA_DIR`**:`alembic.ini` 里的 `sqlalchemy.url` 写死为
  `./data/calendar.db`,手动迁移永远打在真实库上。要在副本上试迁移,得另写一份临时 ini,
  别再拿它当沙箱(本轮已踩过一次)。
- **容器在跑时不要从宿主机改数据库**:宿主机 alembic 与容器内应用同时写同一个 SQLite 文件,
  会产生撕裂的 schema 提交(schema 记录指向不存在的页),容器随即崩溃循环。
  部署顺序应为「停容器 → 改库/迁移 → 起容器」,或干脆只让容器启动时的迁移来做。
  本轮事故与无损恢复的完整记录见 `docs/验收测试记录.md`。

## 已决策:不做

### 管理员读/改成员的私人日历(2026-10-08 决定)

明确**不做**。家长账号是「家庭管理员」而不是「超级用户」:他能管账号、邀请码、共享日历、
应用设置与收件人,但**读不到**任何成员的私人日历(读 404、写 403),导出、邮件、Agent 令牌
与页面一律按调用者的可见范围裁剪。理由:

- 隐私是这次改造的核心需求;一旦开一个"管理员可读"的口子,「私人日历」就只剩文案意义,
  而成员没有任何技术手段能察觉被读;
- 真的需要共享,正确的路径是把那条日历**显式**共享(复用 `calendar_members`,需要时再做),
  而不是靠身份偷偷绕过;
- 忘记口令的补救路径是管理员重置口令(拿到一次性临时口令),不需要读对方的数据。

若将来要开,必须同时给出**成员可见的告知**(例如自己的日历被谁读过),否则不要动这条边界。

### 远程删除日程(2026-09-29 决定)

评估过 `POST /api/agent/events/{id}/delete`(配合 `confirm` + 幂等短路 + 审计 + `restore`)这一组
方案,结论是**删除只保留在电脑端**,理由:

- 删除会立刻向全部同步人员发「日程已移除:标题」邮件,误删会打扰到人;
- 软删除记录实际上只能靠整库备份回滚,没有单事件恢复入口,远程删除的代价不对称;
- 现状本身就是安全边界:`/api/agent` 只有创建与回读两个路由,`/api/v1/events/{id}` 的 DELETE
  需要会话 Cookie + CSRF(不接受 Bearer),本服务在 Tailscale 上只暴露 `/calendar` 与 `/agent`
  两段(2026-10-12 起),因此
  **远程无法删除任何事件**。这条边界不要因为「顺手」而打破。

若将来确实要开,提醒三点:必须 `confirm` 必填、重复删除要幂等短路(否则可能重复发信)、
以及先决定权限范围——「能删任意事件」会同时要求放开 Agent 的读/搜索范围,把 token 泄漏的
影响面从「能建日程」扩大到「能读整个日历 + 能删任何日程」。

### 令牌明文可找回

平台内令牌只存哈希,界面**不能**再次显示明文。这是有意的:能回显就意味着明文进了数据库,
备份泄漏即等于令牌泄漏。丢了就撤销重建(成本只是一次复制粘贴)。

## 已知行为(未改)

- **删除后用同一个 `client_ref` 重新创建**:会命中幂等重放,返回 `200` 且 `duplicate: true`,
  payload 里那条事件带非空 `deleted_at`(它已不在日历里)。符合「命中 client_ref 不新建」的
  不变量,但调用方容易误判;重建请换一个 `client_ref`。
- **接口报错文案**:pydantic 的字段级错误(如未知字段 `duration_minute`)保持英文原文
  `Extra inputs are not permitted`,不做单复数容错——拼错参数应当失败,不应被接口猜中。

