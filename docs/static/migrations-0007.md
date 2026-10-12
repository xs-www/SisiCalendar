# `migrations/versions/0007_email_verification.py`

本迁移给 `users` 加出**邮箱验证状态**所需的 5 列,并把升级前**已经填了邮箱**的账号
一律置为「已验证」(`email_verified_at = created_at`)。数据步骤只在迁移执行到 `0007`
那一次发生,之后的日常维护(登记固定订阅地址、同步收件人等)仍由启动引导负责
(见 `docs/static/app-bootstrap.md`)。

## 背景(用户实测反馈)

账号只填了邮箱并不代表那个邮箱真的属于本人,而**固定的订阅地址**要发到邮箱里、
日历快照也要发到邮箱里。因此:

- 新注册**必须填邮箱**,并在注册流程里用 SMTP 发一次 6 位验证码;
- 成员在验证通过前不能使用系统(页面 303 → `/verify-email`,`/api` 403
  `email_not_verified`;管理员豁免,便于本机救急);
- 验证通过后再发一封邮件,告知其**固定订阅地址**(`AGENTS.md` 不变量 22、23)。

加列的难点在**升级兼容**:真机上这些老邮箱都是本人提供并已在使用(邮件一直在往那里发),
迁移时必须置位,否则升级完成后所有老成员会立刻被闸门挡在验证页外。

## 元信息

- **Revision ID**:`0007_email_verification`(`migrations/versions/0007_email_verification.py:31`)
- **Revises**:`0006_recipient_scope`(`migrations/versions/0007_email_verification.py:32`)
- **Create Date**:2026-10-12
- **入口**:`upgrade()` 在 `migrations/versions/0007_email_verification.py:45`,
  `downgrade()` 在 `migrations/versions/0007_email_verification.py:59`
- **模型对应**:`app/models.py:141`–`app/models.py:146`(`User` 的 5 列),
  后者供 `Base.metadata.create_all` 建库(测试库走这条路)

## 新增

### 列组 `VERIFY_COLUMNS`(加到 `users`)

- **位置**:`migrations/versions/0007_email_verification.py:36`–`migrations/versions/0007_email_verification.py:42`
- **列定义**:

  | 列 | 类型 | 可空 / 默认 | 语义 |
  |---|---|---|---|
  | `email_verified_at` | `DateTime` | 可空 | 验证通过的时刻;`NULL` = 待验证。**唯一的验证判据** |
  | `email_verify_code_hash` | `String(128)` | 可空 | 当前验证码的 HMAC-SHA256 hex;`NULL` = 没有在途验证码 |
  | `email_verify_expires_at` | `DateTime` | 可空 | 验证码失效时刻(naive UTC) |
  | `email_verify_attempts` | `Integer` | `NOT NULL`,`server_default="0"` | 已错误次数,重发时清零 |
  | `email_verify_sent_at` | `DateTime` | 可空 | 最近一次发送时刻,重发冷却据此计算 |

- **注意**:**`batch_alter_table` 只加列,不携带外键**(与 0005/0006 的加列一致);
  这 5 列本来就只引用 `users` 自己,不存在 `ON DELETE SET NULL` 的差异。
  `email_verify_attempts` 带 `server_default="0"`,因此加列时已存在的行会自动填 0,
  不需要额外 `UPDATE`
- **不变量**:库里**只有**验证码的 HMAC,永远不存 6 位明文;明文只允许出现在邮件正文里,
  日志与审计都不得出现(`AGENTS.md` 不变量 22)

## 函数

### `upgrade() -> None`

- **位置**:`migrations/versions/0007_email_verification.py:45`
- **用途**:batch 加 5 列,再把升级前已有非空邮箱的账号回填为「已验证」
- **参数**:无(Alembic 从环境取连接,回填用 `op.get_bind()` 拿 `sa.Connection`)
- **返回**:`None`
- **副作用**:`batch_alter_table("users")` 逐个 `add_column`(`:46`–`:48`);
  一条 `UPDATE users`(`:51`–`:56`)
- **数据步骤**:
  1. `:46`–`:48`:按 `VERIFY_COLUMNS` 顺序加列;
  2. `:51`–`:56`:`UPDATE users SET email_verified_at = created_at
     WHERE email IS NOT NULL AND TRIM(email) <> '' AND email_verified_at IS NULL`
     ——**只**回填「有非空白邮箱」且「尚未置位」的行,回填值取该账号的 `created_at`
     (不需要挂钟时间,保证迁移可重复执行且结果确定)。
- **不变量**:
  - 回填**只**发生在这条迁移里,不得搬进启动引导(`AGENTS.md` 不变量 19:
    结构变更只写在迁移里,引导只做数据回填,且必须幂等);
  - 「已有邮箱视为已验证」是**升级一次的例外**,不能变成每次启动都执行——
    否则新注册的账号会在下一次启动时被自动放行,闸门形同虚设;
  - `downgrade()` 之后再次 `upgrade()`,回填结果与第一次一致(条件里带了
    `email_verified_at IS NULL`,且值来自 `created_at`,不依赖执行时刻)。
- **测试**:暂无直接测试(见下「没有测试覆盖的点」)

### `downgrade() -> None`

- **位置**:`migrations/versions/0007_email_verification.py:59`
- **行为**:按 `VERIFY_COLUMNS` 的**逆序**逐个 `drop_column`(`:60`–`:62`)
- **参数**:无
- **返回**:`None`
- **副作用**:`DROP COLUMN` × 5
- **破坏性说明**:降级**只删列,不回填**——验证状态与在途验证码一并消失。
  降回 `0006` 的代码不再有邮箱闸门,所有账号立刻恢复「能用」;
  但**已经开始依赖验证的数据不会复原**(例如「未验证账号不是收件人」这条规则
  在旧代码里不存在,`sync_recipients` 里那些因未验证而没建的行仍然没有)。
  回退前先备份(`app/cli.py` 的备份命令)。
- **不变量**:降级不得改动 `sync_recipients` 或任何其它表;
  只按列名删自己加的这 5 列
- **测试**:暂无直接测试

## 已知测试盲区

- `upgrade()` 的加列与回填 SQL、`downgrade()` 的删列**都没有自动化测试**:
  测试库由 `tests/conftest.py:74` 的 `Base.metadata.create_all` 建出,**不执行迁移**
  (`tests/conftest.py:121` 有同样的说明),所以「迁移路径」与「建库路径」的差异
  在测试里看不出来。
- 「已有邮箱回填为已验证」这条**最关键**的数据步骤没有用例:
  真机升级已在 Docker 里实测(升级后老成员仍能正常登录、发信),
  但回归测试无法覆盖。
- 降级后再次升级的幂等性(回填条件里的 `email_verified_at IS NULL`)没有被断言。
- 与 0006 的关系只体现在 `down_revision` 上,没有「从 0005 直接跳到 0007」的链路测试。

## 变更记录

- 2026-10-12:新增本文档。归档 0007 的背景(邮箱即收信地址与固定订阅地址的投递点)、
  5 个验证状态列的加列(`batch_alter_table`,尝试次数带 `server_default="0"`)、
  「升级前已有非空邮箱的账号一律回填 `email_verified_at = created_at`」的理由
  (否则老成员会被新的验证闸门挡在门外)、`downgrade()` 只删列不回填的破坏性,
  以及「迁移 DDL 无自动化测试、测试库走 `create_all`、真机升级已在 Docker 实测」的盲区。