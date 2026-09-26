"""后台任务:ICS 修复、邮件队列轮询、中断任务恢复与快照清理。

使用 APScheduler 的单线程后台调度器。应用只运行一个进程,任务之间用数据库状态
做幂等,因此重启后不会重复发送已成功的邮件。
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import session_scope
from app.exporter import cleanup_snapshots
from app.mailqueue import process_queue, recover_stuck_deliveries
from app.settings_store import effective_settings
from app.sync_service import repair_if_needed
from app.timeutil import utcnow

logger = logging.getLogger(__name__)


def maintenance_tick(session: Session, settings: Settings) -> dict[str, object]:
    """一次维护循环,可被测试直接调用。"""
    result: dict[str, object] = {}
    effective = effective_settings(session, settings)

    repaired = repair_if_needed(session, settings=effective)
    result["export_ok"] = repaired.ok
    result["export_version"] = repaired.version
    if not repaired.ok:
        result["export_error"] = repaired.error

    result["recovered"] = recover_stuck_deliveries(session, settings=effective)
    summary = process_queue(session, settings=effective, limit=20)
    result["mail"] = summary.as_dict()
    return result


def daily_tick(session: Session, settings: Settings) -> dict[str, object]:
    effective = effective_settings(session, settings)
    removed = cleanup_snapshots(session, settings=effective)
    cutoff = utcnow() - timedelta(days=effective.backup_retention_days)
    result: dict[str, object] = {"snapshots_removed": removed, "backup_cutoff": cutoff.isoformat()}
    try:
        from app.backup import prune_backups

        result["backups_removed"] = prune_backups(effective)
    except Exception as exc:  # noqa: BLE001 - 备份清理失败不应影响主流程
        logger.warning("备份清理失败:%s", exc)
    return result


class SchedulerService:
    """包装 APScheduler,便于应用生命周期管理与测试。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._scheduler: BackgroundScheduler | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._scheduler is not None and self._scheduler.running

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            scheduler = BackgroundScheduler(
                timezone="UTC",
                job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 30},
            )
            interval = max(15, self.settings.scheduler_interval_seconds)
            scheduler.add_job(
                self._run_maintenance,
                "interval",
                seconds=interval,
                id="maintenance",
                next_run_time=utcnow() + timedelta(seconds=interval),
            )
            scheduler.add_job(
                self._run_daily,
                "interval",
                hours=24,
                id="daily",
                next_run_time=utcnow() + timedelta(minutes=5),
            )
            scheduler.start()
            self._scheduler = scheduler
            logger.info("后台任务已启动,间隔 %s 秒", interval)

    def shutdown(self, wait: bool = True) -> None:
        with self._lock:
            if self._scheduler is None:
                return
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
            logger.info("后台任务已停止")

    def run_once(self) -> dict[str, object]:
        """同步执行一次维护循环,供测试或命令行使用。"""
        return self._run_maintenance()

    def _run_maintenance(self) -> dict[str, object]:
        try:
            with session_scope() as session:
                result = maintenance_tick(session, self.settings)
            if result.get("mail", {}).get("attempted"):  # type: ignore[union-attr]
                logger.info("邮件队列处理结果:%s", result["mail"])
            return result
        except Exception:  # noqa: BLE001 - 后台任务不得抛出到调度器
            logger.exception("维护任务执行失败")
            return {"ok": False}

    def _run_daily(self) -> dict[str, object]:
        try:
            with session_scope() as session:
                return daily_tick(session, self.settings)
        except Exception:  # noqa: BLE001
            logger.exception("每日清理任务失败")
            return {"ok": False}


_service: SchedulerService | None = None


def get_scheduler(settings: Settings | None = None) -> SchedulerService:
    global _service
    if _service is None:
        _service = SchedulerService(settings or get_settings())
    return _service


def reset_scheduler() -> None:
    global _service
    if _service is not None:
        _service.shutdown(wait=False)
    _service = None
