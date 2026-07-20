from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from kalshi_predictor.r5_recovery7 import estimate_progress, sqlite_check, staged_verification


def make_database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO sample(value) VALUES (?)", (("x" * 500,) for _ in range(500))
        )
        connection.commit()
    finally:
        connection.close()
    return path


def test_valid_database_passes_staged_full_certification(tmp_path: Path) -> None:
    report = staged_verification(make_database(tmp_path / "valid.db"))
    assert report["status"] == "certified"
    assert [row["stage"] for row in report["stages"]] == [
        "backup_complete",
        "quick_check",
        "sha256",
        "integrity_check",
        "certified",
    ]


def test_corrupt_database_fails_before_hash(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database" * 100)
    report = staged_verification(path)
    assert report["status"] == "failed"
    assert report["failed_stage"] == "quick_check"
    assert "sha256" not in [row["stage"] for row in report["stages"]]


def test_truncated_database_is_rejected(tmp_path: Path) -> None:
    source = make_database(tmp_path / "source.db")
    truncated = tmp_path / "truncated.db"
    truncated.write_bytes(source.read_bytes()[: source.stat().st_size // 2])
    assert sqlite_check(truncated, "quick_check")["status"] == "failed"


def test_wal_sidecar_does_not_replace_main_file_certification(tmp_path: Path) -> None:
    path = make_database(tmp_path / "wal.db")
    (tmp_path / "wal.db-wal").write_bytes(b"untrusted-sidecar")
    report = staged_verification(path)
    assert report["status"] == "certified"
    assert report["database_path"].endswith("wal.db")


def test_copy_of_valid_database_has_same_full_result(tmp_path: Path) -> None:
    source = make_database(tmp_path / "source.db")
    copied = tmp_path / "copied.db"
    shutil.copy2(source, copied)
    assert sqlite_check(source, "integrity_check")["status"] == "ok"
    assert sqlite_check(copied, "integrity_check")["status"] == "ok"


def test_progress_estimate_advancing_io_is_not_stale() -> None:
    progress = estimate_progress(
        database_bytes=1_000,
        read_bytes_start=100,
        read_bytes_current=600,
        elapsed_seconds=10,
        previous_read_bytes=500,
        previous_sample_age_seconds=600,
    )
    assert progress["progress_percent_lower_bound"] == 50.0
    assert progress["stale"] is False
    assert progress["safe_to_interrupt"] is False


def test_progress_requires_no_io_for_bounded_stale_period() -> None:
    recent = estimate_progress(
        database_bytes=1_000,
        read_bytes_start=0,
        read_bytes_current=100,
        elapsed_seconds=10,
        previous_read_bytes=100,
        previous_sample_age_seconds=299,
    )
    stale = estimate_progress(
        database_bytes=1_000,
        read_bytes_start=0,
        read_bytes_current=100,
        elapsed_seconds=10,
        previous_read_bytes=100,
        previous_sample_age_seconds=300,
    )
    assert recent["stale"] is False
    assert stale["stale"] is True
    assert stale["safe_to_interrupt"] is False
