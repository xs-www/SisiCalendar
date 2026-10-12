"""运维命令行的安全网:提醒「操作的其实是另一个数据库」。"""

from __future__ import annotations

import logging
from pathlib import Path

from app import cli
from app.config import Settings


def _settings_for(data_dir: Path) -> Settings:
    return Settings().model_copy(update={"data_dir": data_dir})


def _fake_repo(tmp_path: Path) -> Path:
    """造一个「仓库」:里面有 ./data/calendar.db。"""
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "calendar.db").write_bytes(b"SQLite format 3\x00")
    return repo


def test_cli_warns_when_data_dir_points_elsewhere(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """`.env` 的 DATA_DIR=/data 在宿主机上会指向别的库,必须大声提醒。"""
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()

    with caplog.at_level(logging.WARNING, logger="app.cli"):
        cli._warn_if_container_path(_settings_for(elsewhere))

    messages = "\n".join(record.message for record in caplog.records)
    assert "本次操作的数据库是" in messages
    assert "DATA_DIR" in messages or "盘符路径" in messages


def test_cli_stays_quiet_when_data_dir_is_the_repo_one(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)

    with caplog.at_level(logging.WARNING, logger="app.cli"):
        cli._warn_if_container_path(_settings_for(repo / "data"))

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


def test_cli_stays_quiet_when_repo_has_no_database(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """容器里没有 /app/data,不该天天刷警告。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(cli, "PROJECT_ROOT", repo)

    with caplog.at_level(logging.WARNING, logger="app.cli"):
        cli._warn_if_container_path(_settings_for(tmp_path / "data"))

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []