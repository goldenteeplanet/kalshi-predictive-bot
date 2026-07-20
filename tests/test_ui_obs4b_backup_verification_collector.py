from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.backup_verification_collector import (
    adapt_captured_verification,
    collect_local_verification,
    publish_verification,
)

FIXTURE = Path("tests/fixtures/ui_obs4b/verification_sources.json")


def sources():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["sources"]


def test_running_integrity_process_maps_to_dashboard_schema() -> None:
    result = adapt_captured_verification(sources())
    assert result["state"] == "RUNNING"
    assert result["stage"] == "integrity_check"
    assert result["pid"] == 367918
    assert result["progress_percent_lower_bound"] == 50.0
    assert result["deployment_blocked"] is True
    assert result["collector_diagnostics"] == []


def test_verified_outputs_are_required_for_passed_state() -> None:
    captured = sources()
    captured["process"]["running"] = False
    captured["integrity_output"] = "ok\n"
    captured["sha256_output"] = "a" * 64 + "  /mnt/backup.db\n"
    result = adapt_captured_verification(captured)
    assert result["state"] == "PASSED"
    assert result["deployment_blocked"] is False
    assert result["sha256"] == "a" * 64


def test_malformed_integrity_or_sha_never_passes() -> None:
    captured = sources()
    captured["process"]["running"] = False
    captured["integrity_output"] = "not ok"
    captured["sha256_output"] = "malformed"
    result = adapt_captured_verification(captured)
    assert result["state"] == "FAILED"
    assert result["deployment_blocked"] is True


def test_execution_uncertainty_fails_closed() -> None:
    captured = sources()
    captured["execution"] = "unknown"
    result = adapt_captured_verification(captured)
    assert result["state"] == "BLOCKED"
    assert "EXECUTION_STATE_NOT_DISABLED" in result["collector_diagnostics"]


def test_missing_io_evidence_fails_visible() -> None:
    captured = sources()
    captured["proc_io"] = "rchar: 10"
    result = adapt_captured_verification(captured)
    assert result["state"] == "BLOCKED"
    assert "PROC_IO_READ_BYTES_MISSING" in result["collector_diagnostics"]


def test_previous_snapshot_detects_stale_io_but_never_interrupts() -> None:
    captured = sources()
    captured["previous_sample_age_seconds"] = 300
    previous = {"backup_verification": {"raw_read_bytes": 11_159_486_464}}
    result = adapt_captured_verification(captured, previous_snapshot=previous)
    assert result["stale"] is True
    assert result["safe_to_interrupt"] is False


def test_atomic_publication_has_no_temporary_residue(tmp_path: Path) -> None:
    destination = tmp_path / "verification.json"
    result = publish_verification(
        {"backup_verification": adapt_captured_verification(sources())}, destination
    )
    assert result["published"] is True
    assert result["temporary_absent"] is True
    assert result["database_writes"] == 0
    assert destination.exists()


def test_local_proc_collector_discovers_only_exact_verification_target(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("1000.0 0.0")
    database = tmp_path / "backup.db"
    database.write_bytes(b"sqlite-placeholder")
    process = proc / "42"
    process.mkdir()
    (process / "cmdline").write_bytes(
        f"sqlite3\0{database}\0PRAGMA integrity_check;\0".encode()
    )
    fields = ["0"] * 22
    fields[0] = "42"
    fields[1] = "(sqlite3)"
    fields[21] = "100"
    (process / "stat").write_text(" ".join(fields))
    (process / "io").write_text("read_bytes: 9\n")
    integrity = tmp_path / "integrity.txt"
    sha = tmp_path / "backup.db.sha256"
    integrity.write_text("")
    sha.write_text("")
    result = collect_local_verification(
        database_path=database,
        integrity_output_path=integrity,
        sha256_output_path=sha,
        proc_root=proc,
    )
    assert result["pid"] == 42
    assert result["stage"] == "integrity_check"
    assert result["source_mode"] == "CAPTURED_READ_ONLY"


def test_permission_restricted_io_keeps_healthy_process_running() -> None:
    captured = sources()
    captured["proc_io"] = ""
    captured["proc_io_status"] = "permission_restricted"
    result = adapt_captured_verification(captured)
    assert result["state"] == "RUNNING"
    assert result["progress_available"] is False
    assert result["progress_percent_lower_bound"] is None
    assert result["estimated_remaining_seconds"] is None
    assert result["io_visibility"] == "PERMISSION_RESTRICTED"
    assert result["collector_diagnostics"] == []
    assert result["safe_to_interrupt"] is False


def test_exact_sqlite_child_is_preferred_over_shell_parent(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("1000.0 0.0")
    database = tmp_path / "backup.db"
    database.write_bytes(b"x")
    for pid, command in (
        (10, f"bash\0-c\0sqlite3 {database} PRAGMA integrity_check;"),
        (11, f"sqlite3\0{database}\0PRAGMA integrity_check;"),
    ):
        process = proc / str(pid)
        process.mkdir()
        (process / "cmdline").write_bytes(command.encode())
        fields = ["0"] * 22
        fields[0], fields[1], fields[21] = str(pid), "(process)", "100"
        (process / "stat").write_text(" ".join(fields))
        (process / "io").write_text("read_bytes: 1\n")
    result = collect_local_verification(
        database_path=database,
        integrity_output_path=tmp_path / "none",
        sha256_output_path=tmp_path / "none.sha",
        proc_root=proc,
    )
    assert result["pid"] == 11
