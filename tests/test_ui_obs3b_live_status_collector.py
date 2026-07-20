from pathlib import Path

from kalshi_predictor.ui.live_status_collector import collect_live_snapshot, publish_live_snapshot, storage_status


def runner(command: list[str], timeout: int) -> str:
    assert timeout == 10
    if command[:2] == ["kalshi-bot", "db-writer-monitor"]:
        return "Current writer PID: none\nSafe to start another write job: yes\n"
    if command[:2] == ["kalshi-bot", "db-locks"]:
        return "Database lock diagnostics: CLEAR\nOpen DB holders: none visible\n"
    return "ActiveState=active\nSubState=running\nExecMainPID=42\nResult=success\n"


def test_collector_is_read_only_and_fail_closed(tmp_path: Path) -> None:
    snapshot = collect_live_snapshot(runner=runner, backup_root=tmp_path)
    assert snapshot["execution_enabled"] is False
    assert snapshot["collector"]["database_writes"] == 0
    assert snapshot["writer"]["safe_to_start_write"] is True
    assert snapshot["writer"]["pid"] is None
    assert snapshot["active_process"]["state"] == "RUNNING"


def test_source_failure_blocks_process(tmp_path: Path) -> None:
    def failing(command: list[str], timeout: int) -> str:
        if "db-writer-monitor" in command:
            raise TimeoutError
        return runner(command, timeout)
    snapshot = collect_live_snapshot(runner=failing, backup_root=tmp_path)
    assert snapshot["active_process"]["state"] == "BLOCKED"
    assert snapshot["writer"]["safe_to_start_write"] is False


def test_atomic_publication_and_history(tmp_path: Path) -> None:
    destination = tmp_path / "progress.json"
    result = publish_live_snapshot(collect_live_snapshot(runner=runner, backup_root=tmp_path), destination)
    assert result["published"] is True
    assert destination.exists()
    assert not destination.with_suffix(".json.tmp").exists()
    assert result["history_entries"] == 1


def test_writer_pid_and_verified_backup_display_fields(tmp_path: Path) -> None:
    metadata = tmp_path / "latest.backup.json"
    metadata.write_text('{"backup_path":"/backup/db","integrity_check":"ok","sha256":"abc"}')

    def busy(command: list[str], timeout: int) -> str:
        if command[:2] == ["kalshi-bot", "db-writer-monitor"]:
            return "Current writer PID: 123\nSafe to start another write job: no\n"
        if command[:2] == ["kalshi-bot", "db-locks"]:
            return "Database lock diagnostics: BUSY_WRITER\n"
        return runner(command, timeout)

    snapshot = collect_live_snapshot(runner=busy, backup_root=tmp_path, reports_root=tmp_path)
    assert snapshot["writer"]["pid"] == 123
    assert snapshot["backup"]["sha256_status"] == "VERIFIED"


def test_storage_pressure_is_visible_and_bounded(monkeypatch, tmp_path: Path) -> None:
    usage = type("Usage", (), {"total": 1000, "used": 850, "free": 150})()
    monkeypatch.setattr("kalshi_predictor.ui.live_status_collector.shutil.disk_usage", lambda _path: usage)
    status, alerts = storage_status({"backup": tmp_path})
    assert status["backup"]["state"] == "WARNING"
    assert status["backup"]["free_percent"] == 15.0
    assert alerts[0]["code"] == "STORAGE_BACKUP_WARNING"


def test_storage_failure_fails_visible_not_silent(monkeypatch, tmp_path: Path) -> None:
    def fail(_path):
        raise OSError("unavailable")
    monkeypatch.setattr("kalshi_predictor.ui.live_status_collector.shutil.disk_usage", fail)
    status, alerts = storage_status({"project": tmp_path})
    assert status["project"]["state"] == "UNKNOWN"
    assert alerts[0]["code"] == "STORAGE_PROJECT_UNKNOWN"
