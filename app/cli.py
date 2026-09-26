"""运维命令行:备份、恢复、导出、一致性检查与凭据哈希。

用法:

    python -m app.cli backup
    python -m app.cli restore ./data/backups/calendar-20260101-030000.db
    python -m app.cli export --force
    python -m app.cli verify
    python -m app.cli hash-password
    python -m app.cli process-mail
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from app.backup import create_backup, list_backups, prune_backups, restore_backup
from app.config import Settings, get_settings
from app.db import init_engine, session_scope
from app.exporter import verify_consistency
from app.mailqueue import process_queue, queue_counts
from app.security import ensure_admin_credential, hash_password, load_admin_hash
from app.sync_service import overview, publish, repair_if_needed

logger = logging.getLogger("app.cli")


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
    password = args.password or getpass.getpass("请输入新的管理员口令:")
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

    sub.add_parser("credential", help="查看管理员凭据来源").set_defaults(func=cmd_show_credential)
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = build_parser().parse_args(argv)
    settings = get_settings()
    settings.ensure_directories()
    return int(args.func(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
