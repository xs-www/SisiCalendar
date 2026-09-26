"""数据库引擎与会话管理。

SQLite 启用 WAL,但应用只运行一个进程、一个写入者。引擎在首次使用时按配置
惰性创建,测试可以通过 `init_engine` 指向临时目录。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_engine_for(settings: Settings, *, use_null_pool: bool = False) -> Engine:
    settings.ensure_directories()
    kwargs: dict[str, object] = {
        "echo": False,
        "future": True,
        "connect_args": {"check_same_thread": False, "timeout": 30},
    }
    if use_null_pool:
        kwargs["poolclass"] = NullPool
    engine = create_engine(settings.database_url, **kwargs)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def init_engine(settings: Settings | None = None, *, use_null_pool: bool = False) -> Engine:
    """创建(或替换)全局引擎,返回新引擎。"""
    global _engine, _session_factory
    resolved = settings or get_settings()
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine_for(resolved, use_null_pool=use_null_pool)
    _session_factory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    logger.debug("数据库引擎已初始化:%s", resolved.db_path)
    return _engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        return init_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        init_engine()
    assert _session_factory is not None
    return _session_factory


def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    """FastAPI 依赖:每个请求一个会话。"""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """后台任务使用的事务作用域。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
