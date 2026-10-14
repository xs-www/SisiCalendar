# `migrations/versions/0008_delivery_actor.py`

本迁移给 `mail_deliveries` 加出**发起人**的两列,让同步邮件能说明「这次是谁操作的」。
只加列、不回填:升级前已有的历史任务本来就没有发起人信息,正文与队列都显示「未记录」,
比编造一个名字安全。

## 背景(用户实测反馈)

同步邮件原来只说「本次操作:新增日程」,收信的家人看不出**是谁**改的。邮件是纯文本、
收件端不做换算,所以发起人必须在**建任务那一刻**固化进任务记录,重试时不能再现算
(否则重试发的是「当前登录者」而不是当时那个人)。

## 元信息

- **Revision ID**:`0008_delivery_actor`(`migrations/versions/0008_delivery_actor.py:35`)
- **Revises**:`0007_email_verification`(`migrations/versions/0008_delivery_actor.py:36`)
- **Create Date**:2026-10-12
- **入口**:`upgrade()` 在 `migrations/versions/0008_delivery_actor.py:46`,
  `downgrade()` 在 `migrations/versions/0008_delivery_actor.py:52`
- **模型对应**:`app/models.py:441`–`app/models.py:446`(`MailDelivery` 的两列),
  后者供 `Base.metadata.create_all` 建库(测试库走这条路)

## 新增

### 列组 `ACTOR_COLUMNS`(加到 `mail_deliveries`)

- **位置**:`migrations/versions/0008_delivery_actor.py:40`–`migrations/versions/0008_delivery_actor.py:43`
- **列定义**:

  | 列 | 类型 | 可空 / 默认 | 语义 |
  |---|---|---|---|
  | `actor_user_id` | `String(36)` | 可空 | 发起人的账号 id;发信时按它**现查** `users.display_name or username` |
  | `actor_label` | `String(100)` | 可空 | 发起人显示名的**快照**;账号删除后仍能说明当时是谁 |

- **为什么是两列而不是一列**:
  - 只存 id → 账号被删除后(`app/accounts.py::delete_user` 会把 `actor_user_id` 置空)
    邮件就再也说不出「当时是谁」;
  - 只存名字 → 成员改了显示名之后,还在队列里没发出去的邮件会用旧名字;
  - 两列合起来:`actor_user_id` 优先(名字活),`actor_label` 兜底(账号没了名字还在)。
    实现在 `app/mailqueue.py::_actor_name`。
- **注意**:**`batch_alter_table` 只加列,不携带外键**(与 0005/0006/0007 的加列一致)。
  真机升级上来的库没有外键约束,删账号时靠 `delete_user` **显式**清空,
  否则会留下指向已删除账号的悬空 `actor_user_id`(`AGENTS.md` 不变量 20 的同一条理由)。
  测试库走 `create_all`,模型里也**故意不声明** `ForeignKey`,
  这样两条建库路径的行为完全一致。
- **不变量**:发起人**不得**用 `trigger_event_id` 反查事件的 `created_by_user_id` 来猜 ——
  那是「谁建的」而不是「谁改的」,猜错会把别人的改动记到创建人头上
  (`app/sync_service.py::publish` 的 docstring 明确写了这一点)

## 函数

### `upgrade() -> None`

- **位置**:`migrations/versions/0008_delivery_actor.py:46`
- **用途**:batch 加两列
- **参数**:无(Alembic 从环境取连接)
- **返回**:`None`
- **副作用**:`batch_alter_table("mail_deliveries")` 逐个 `add_column`(`:47`–`:49`)
- **数据步骤**:**没有**。历史任务的发起人无法还原,留空比猜一个更安全;
  正文与队列对空值分别渲染成「未记录」与「—」
- **不变量**:迁移只做结构变更,不做数据回填(`AGENTS.md` 不变量 19:结构变更只写在迁移里);
  升级后**不需要**跑任何引导动作

### `downgrade() -> None`

- **位置**:`migrations/versions/0008_delivery_actor.py:52`
- **行为**:按 `ACTOR_COLUMNS` 的**逆序**逐个 `drop_column`(`:53`–`:55`)
- **参数**:无
- **返回**:`None`
- **副作用**:`DROP COLUMN` × 2
- **破坏性说明**:降级**只删列**——所有历史任务丢失「谁操作的」这层说明(不影响发信本身)
- **不变量**:降级不得改动 `sync_recipients`、`calendar_exports` 或任何其它表;
  只按列名删自己加的这两列;降回 `0007` 的代码不引用这两列,发信功能照常
- **测试**:暂无直接测试(见下)

## 已知测试盲区

- `upgrade()` 的加列与 `downgrade()` 的删列**都没有自动化测试**:测试库由
  `tests/conftest.py::engine` 的 `Base.metadata.create_all` 建出,**不执行迁移**
  (`tests/conftest.py:121` 有同样的说明)。
  执行证据只有两处观测:① `uv run pytest` 的 lifespan 日志里出现
  `Running upgrade 0007_email_verification -> 0008_delivery_actor`;
  ② **2026-10-13 真机部署**时容器启动日志出现同一条,且
  `alembic_version = 0008_delivery_actor`、`mail_deliveries` 确实含 `actor_user_id` /
  `actor_label`(见 `docs/验收测试记录.md` §3.7、§3.8)。
  仍缺的是「升级前的库 → `upgrade()` → 断言列存在」这类**可重复**的自动化用例。
- 「加列后历史任务的 `actor_*` 为空」这条行为有用例覆盖,但走的是 `create_all` 建出的表
  (`tests/test_mail_actor.py::test_legacy_task_without_actor_says_unrecorded`)。
- 降级后再次升级的幂等性(Alembic 自身的版本表保证)没有被断言。
- `downgrade()` **从未在真机或测试里执行过**——它只删列,风险低但确实没有执行记录。

## 变更记录

- 2026-10-12:新增本文档。归档 0008 的背景(用户反馈「同步邮件看不出是谁改的」)、
  两列的分工(现查账号 + 名字快照)、只加列不回填的决定、
  「不带着外键、删账号由应用层显式置空」与 0005–0007 的一致做法,以及迁移 DDL 的测试盲区。
- 2026-10-13:补记真机执行证据(`alembic_version` 与两列已在生产库落地,
  见 `docs/验收测试记录.md` §3.7/§3.8),并把盲区表述收紧为「缺自动化用例 + `downgrade()` 从未执行」。
