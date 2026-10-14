"""运维命令行:备份、恢复、导出、一致性检查与凭据哈希。

用法:

    python -m app.cli backup
    python -m app.cli restore ./data/backups/calendar-20260101-030000.db
    python -m app.cli export --force
    python -m app.cli verify
    python -m app.cli hash-password
    python -m app.cli process-mail
    python -m app.cli bootstrap
    python -m app.cli bootstrap --rotate
    python -m app.cli reset-admin-password
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import secrets
import sys
from pathlib import Path

from app.backup import create_backup, list_backups, prune_backups, restore_backup
from app.config import Settings, get_settings
from app.db import init_engine, session_scope
from app.exporter import verify_consistency
from app.mailqueue import process_queue, queue_counts
from app.security import ensure_admin_credential, hash_password, load_admin_hash
from app.sync_service import overview, publish, repair_if_needed

logger = logging.getLogger("app.cli")

#: 仓库根目录(提示用户用盘符路径指向 `./data` 时用)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def _prepare(settings: Settings) -> None:
    """命令行操作前先保证数据库结构存在。"""
    from app.bootstrap import bootstrap_database

    bootstrap_database(settings)


def cmd_backup(args: argparse.Namespace, settings: Settings) -> int:
    _prepare(settings)
    path = create_backup(settings)
    removed = prune_backups(settings)
    print(json.dumps({"backup": str(path), "pruned": removed}, ensure_ascii=False))
    return 0


def cmd_backups(args: argparse.Namespace, settings: Settings) -> int:
    entries = [str(path) for path in list_backups(settings)]
    print(json.dumps({"backups": entries, "count": len(entries)}, ensure_ascii=False))
    return 0


def cmd_restore(args: argparse.Namespace, settings: Settings) -> int:
    from pathlib import Path

    source = Path(args.path)
    target = restore_backup(source, settings)
    init_engine(settings)
    with session_scope() as session:
        outcome = publish(session, settings=settings, force_new_version=True)
        ok, detail = verify_consistency(session, settings=settings)
    print(
        json.dumps(
            {
                "restored_to": str(target),
                "export": outcome.as_dict(),
                "consistency": {"ok": ok, "detail": detail},
                "hint": "容器或本机服务必须处于停止状态时才能恢复数据库。",
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_export(args: argparse.Namespace, settings: Settings) -> int:
    _prepare(settings)
    with session_scope() as session:
        outcome = publish(
            session, settings=settings, force_new_version=bool(args.force)
        )
    print(json.dumps(outcome.as_dict(), ensure_ascii=False))
    return 0 if outcome.ok else 1


def cmd_verify(args: argparse.Namespace, settings: Settings) -> int:
    _prepare(settings)
    with session_scope() as session:
        repaired = repair_if_needed(session, settings=settings)
        ok, detail = verify_consistency(session, settings=settings)
        status = overview(session, settings=settings)
    print(
        json.dumps(
            {
                "repair": repaired.as_dict(),
                "consistency": {"ok": ok, "detail": detail},
                "state": {
                    "version": status["version"],
                    "dirty": status["dirty"],
                    "size_bytes": status["size_bytes"],
                    "file_exists": status["file_exists"],
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if ok else 1


def cmd_process_mail(args: argparse.Namespace, settings: Settings) -> int:
    _prepare(settings)
    with session_scope() as session:
        summary = process_queue(session, settings=settings, limit=int(args.limit))
        counts = queue_counts(session)
    print(json.dumps({"summary": summary.as_dict(), "counts": counts}, ensure_ascii=False))
    return 0


def cmd_hash_password(args: argparse.Namespace, settings: Settings) -> int:
    password = args.password or getpass.getpass("请输入新的家庭管理员口令:")
    if not password:
        print("口令不能为空", file=sys.stderr)
        return 2
    digest = hash_password(password)
    print(digest)
    print(
        "\n把它写入 .env 的 ADMIN_PASSWORD_HASH,并删除 ADMIN_PASSWORD 与 data/admin.hash。",
        file=sys.stderr,
    )
    return 0


def cmd_show_credential(args: argparse.Namespace, settings: Settings) -> int:
    generated = ensure_admin_credential(settings)
    stored = load_admin_hash(settings)
    payload = {
        "username": settings.admin_username,
        "hash_source": (
            "env:ADMIN_PASSWORD_HASH"
            if settings.admin_password_hash
            else "env:ADMIN_PASSWORD"
            if settings.admin_password
            else "data/admin.hash"
        ),
        "has_hash": bool(stored or generated),
    }
    if generated:
        payload["generated_password"] = generated
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def cmd_bootstrap(args: argparse.Namespace, settings: Settings) -> int:
    """查看(必要时生成)首位管理员的引导码与完整引导地址。

    首位管理员的口令若是随机生成的,明文只在启动日志里出现过一次;引导码文件
    是**不依赖日志**的那条路(见 `app/bootstrap_access.py`)。
    """
    _prepare(settings)
    from app.bootstrap_access import bootstrap_url, code_path, ensure_code, pending_owner

    with session_scope() as session:
        owner = pending_owner(session)
        if owner is None:
            print(
                json.dumps(
                    {
                        "status": "not_pending",
                        "hint": (
                            "首位管理员的口令不是初始口令,引导通道已关闭。"
                            "忘记口令请用 `python -m app.cli reset-admin-password`。"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        code = ensure_code(settings, rotate=bool(args.rotate))
        username = owner.username

    print(
        json.dumps(
            {
                "status": "pending",
                "username": username,
                "code_file": str(code_path(settings)),
                "code": code,
                "url": bootstrap_url(settings, code),
                "note": "口令设置成功后引导码文件会自动删除,该地址随即失效。",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_reset_admin_password(args: argparse.Namespace, settings: Settings) -> int:
    """给首位管理员直接设置新口令(口令彻底丢失时的救援通道)。

    `--generate` 生成一枚一次性随机口令并把 `must_change_password` 置真
    (登录后会被强制改密);不给 `--generate` 时交互式输入一个自己记得住的口令。
    """
    _prepare(settings)
    from app.accounts import AccountError, owner_user, set_password
    from app.bootstrap_access import clear_code, ensure_code, pending_owner

    with session_scope() as session:
        owner = owner_user(session)
        if owner is None:  # pragma: no cover - 引导保证存在
            print("数据库里没有任何账号", file=sys.stderr)
            return 1
        if args.generate:
            password = secrets.token_urlsafe(12)
            must_change = True
        else:
            password = args.password or getpass.getpass("请输入新的管理员口令:")
            must_change = False
        try:
            set_password(session, owner, password, must_change=must_change)
        except AccountError as exc:
            print(";".join(exc.fields.values()), file=sys.stderr)
            return 2
        # 与新的 must_change_password 对齐:还需要改密 → 保住引导码;已定稿 → 删掉
        if pending_owner(session) is not None:
            ensure_code(settings)
        else:
            clear_code(settings)
        payload: dict[str, object] = {
            "username": owner.username,
            "must_change": must_change,
        }
        if args.generate:
            payload["generated_password"] = password

    print(json.dumps(payload, ensure_ascii=False))
    if args.generate:
        print(
            "\n上面的一次性口令只显示这一次;用它登录后必须立刻改成自己的口令。",
            file=sys.stderr,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="SisiCalendar 运维命令")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup", help="生成一份一致的 SQLite 备份").set_defaults(func=cmd_backup)
    sub.add_parser("backups", help="列出已有备份").set_defaults(func=cmd_backups)

    restore = sub.add_parser("restore", help="从备份恢复数据库并重新生成 ICS")
    restore.add_argument("path", help="备份文件路径")
    restore.set_defaults(func=cmd_restore)

    export = sub.add_parser("export", help="重新生成 calendar.ics")
    export.add_argument("--force", action="store_true", help="内容未变化也生成新版本")
    export.set_defaults(func=cmd_export)

    sub.add_parser("verify", help="一致性检查与自动修复").set_defaults(func=cmd_verify)

    mail = sub.add_parser("process-mail", help="立即处理一次发信队列")
    mail.add_argument("--limit", type=int, default=50)
    mail.set_defaults(func=cmd_process_mail)

    hasher = sub.add_parser("hash-password", help="生成 Argon2id 口令哈希")
    hasher.add_argument("--password", default="", help="不推荐:会留在命令历史中")
    hasher.set_defaults(func=cmd_hash_password)

    boot = sub.add_parser(
        "bootstrap", help="查看/生成首位管理员的引导码与引导地址(初始口令救援)"
    )
    boot.add_argument(
        "--rotate", action="store_true", help="重新生成引导码(旧码立即失效)"
    )
    boot.set_defaults(func=cmd_bootstrap)

    reset = sub.add_parser(
        "reset-admin-password", help="给首位管理员直接设置新口令(口令丢失时的救援)"
    )
    reset.add_argument("--password", default="", help="不推荐:会留在命令历史中")
    reset.add_argument(
        "--generate", action="store_true", help="生成一次性随机口令(登录后强制修改)"
    )
    reset.set_defaults(func=cmd_reset_admin_password)

    sub.add_parser("credential", help="查看管理员凭据来源").set_defaults(func=cmd_show_credential)
    return parser


def _warn_if_container_path(settings: Settings) -> None:
    """提醒「命令行操作的其实是另一个数据库」。

    `.env` 里的 `DATA_DIR=/data` 是为容器准备的;在 Windows 宿主机上直接跑 CLI 时,
    `/data` 会被解析成**当前盘符根目录**下的 `\\data`(例如 `D:\\data`),于是命令会
    悄悄操作另一个(可能是空的)数据库——备份看起来成功了,备的却不是线上那份。
    判据:仓库里存在 `./data/calendar.db`,而这次用的 `DATA_DIR` 指向别处。
    """
    repo_db = PROJECT_ROOT / "data" / "calendar.db"
    try:
        same = Path(settings.data_dir).resolve() == repo_db.parent.resolve()
    except OSError:  # pragma: no cover - 路径异常时不做判断
        same = True
    if repo_db.exists() and not same:
        logger.warning(
            "本次操作的数据库是 %s,而仓库里另有一份 %s。",
            settings.database_url,
            repo_db,
        )
        logger.warning(
            "若你本想操作仓库/线上那份,请显式指定盘符路径:"
            "$env:DATA_DIR='%s'; uv run python -m app.cli ...",
            repo_db.parent,
        )
    else:
        logger.info("数据库:%s", settings.database_url)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    _warn_if_container_path(settings)
    settings.ensure_directories()
    return int(args.func(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
