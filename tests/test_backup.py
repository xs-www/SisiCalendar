"""备份与恢复测试:对应验收标准第 13 条。"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import timedelta
from pathlib import Path

from app import db as db_module
from app.backup import (
    BACKUP_PREFIX,
    BACKUP_SUFFIX,
    create_backup,
    list_backups,
    prune_backups,
    restore_backup,
)
from app.config import Settings
from app.events import create_event, soft_delete_event, update_event
from app.exporter import export_calendar
from app.models import Base, Event
from app.schemas import EventWrite
from app.timeutil import utcnow


def _seed(session, settings: Settings) -> tuple[str, str]:
    """写入两条事件并提交,返回两个 UID。"""
    first = create_event(
        session,
        EventWrite(
            title="备份一号",
            start_at="2025-03-04T09:00:00",
            end_at="2025-03-04T10:00:00",
            timezone="Australia/Brisbane",
        ),
        settings=settings,
    )
    second = create_event(
        session,
        EventWrite(
            title="备份二号",
            start_at="2025-04-04T09:00:00",
            end_at="2025-04-04T10:00:00",
            timezone="Australia/Brisbane",
        ),
        settings=settings,
    )
    session.commit()
    update_event(session, second, EventWrite(title="备份二号(已改)"), settings=settings)
    session.commit()
    export_calendar(session, settings=settings)
    session.commit()
    return first.uid, second.uid


def _snapshot(session) -> dict[str, tuple[str, int, bool]]:
    return {
        event.id: (event.uid, event.sequence, event.deleted_at is not None)
        for event in session.query(Event).all()
    }


def _open_session():
    return db_module.get_session_factory()()


def test_backup_is_a_consistent_sqlite_snapshot(engine, settings: Settings) -> None:
    session = _open_session()
    try:
        _seed(session, settings)
        expected = _snapshot(session)
    finally:
        session.close()

    backup = create_backup(settings)
    assert backup.exists()
    assert backup.name.startswith(BACKUP_PREFIX) and backup.suffix == BACKUP_SUFFIX

    connection = sqlite3.connect(str(backup))
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"events", "sync_recipients", "mail_deliveries", "calendar_exports"} <= tables
        rows = list(connection.execute("SELECT id, uid, sequence FROM events"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()

    assert integrity == "ok"
    assert {row[0]: (row[1], row[2], False) for row in rows} == expected


def test_restore_returns_to_backup_state(engine, settings: Settings) -> None:
    session = _open_session()
    try:
        first_uid, second_uid = _seed(session, settings)
        before = _snapshot(session)
    finally:
        session.close()

    backup = create_backup(settings)

    # 备份之后继续改动:删除一条、新增一条、再生成一次 ICS
    session = _open_session()
    try:
        target = session.query(Event).filter(Event.uid == first_uid).one()
        soft_delete_event(session, target)
        create_event(
            session,
            EventWrite(
                title="备份之后新增",
                start_at="2025-05-04T09:00:00",
                end_at="2025-05-04T10:00:00",
                timezone="UTC",
            ),
            settings=settings,
        )
        session.commit()
        export_calendar(session, settings=settings)
        session.commit()
    finally:
        session.close()

    db_module.dispose_engine()
    restore_backup(backup, settings)
    assert Path(str(settings.db_path) + "-wal").exists() is False

    db_module.init_engine(settings)
    Base.metadata.create_all(db_module.get_engine())

    session = _open_session()
    try:
        after = _snapshot(session)
        titles = {event.title for event in session.query(Event).all()}
        result = export_calendar(session, settings=settings, force_new_version=True)
        session.commit()
    finally:
        session.close()

    assert after == before, "恢复后的事件、UID 与 sequence 必须与备份时一致"
    assert second_uid in {uid for uid, _, _ in after.values()}
    assert "备份之后新增" not in titles
    assert result.path.exists()

    feed = result.path.read_bytes().decode("utf-8")
    assert "备份一号" in feed
    assert "备份之后新增" not in feed


def test_restore_keeps_pre_restore_copy(engine, settings: Settings) -> None:
    session = _open_session()
    try:
        _seed(session, settings)
    finally:
        session.close()
    backup = create_backup(settings)

    db_module.dispose_engine()
    restore_backup(backup, settings)

    pre_restore = Path(str(settings.db_path) + ".pre-restore")
    assert pre_restore.exists(), "恢复前必须留下可回退的副本"
    db_module.init_engine(settings)


def test_prune_backups_removes_expired_files(engine, settings: Settings) -> None:
    session = _open_session()
    try:
        _seed(session, settings)
    finally:
        session.close()

    fresh = create_backup(settings)
    old = create_backup(settings)
    stale_time = time.time() - (settings.backup_retention_days + 5) * 86400
    os.utime(old, (stale_time, stale_time))

    removed = prune_backups(settings)
    assert removed == 1
    remaining = list_backups(settings)
    assert [path.name for path in remaining] == [fresh.name]


def test_restore_missing_file_raises(engine, settings: Settings) -> None:
    import pytest

    db_module.dispose_engine()
    with pytest.raises(FileNotFoundError):
        restore_backup(settings.backup_dir / "nope.db", settings)
    db_module.init_engine(settings)


def test_backup_without_database_raises(settings: Settings) -> None:
    import pytest

    assert not settings.db_path.exists()
    with pytest.raises(FileNotFoundError):
        create_backup(settings)


def test_deleted_rows_survive_backup_restore(engine, settings: Settings) -> None:
    """软删除的记录也要随备份保留,便于审计。"""
    session = _open_session()
    try:
        first_uid, _ = _seed(session, settings)
        target = session.query(Event).filter(Event.uid == first_uid).one()
        soft_delete_event(session, target)
        session.commit()
        deleted_at = target.deleted_at
    finally:
        session.close()

    backup = create_backup(settings)
    db_module.dispose_engine()
    restore_backup(backup, settings)
    db_module.init_engine(settings)

    session = _open_session()
    try:
        restored = session.query(Event).filter(Event.uid == first_uid).one()
        assert restored.deleted_at is not None
        assert abs(restored.deleted_at - deleted_at) < timedelta(seconds=1)
        assert restored.deleted_at <= utcnow()
    finally:
        session.close()
