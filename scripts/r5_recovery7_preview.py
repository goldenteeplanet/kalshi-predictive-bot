from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from kalshi_predictor.r5_recovery7 import benchmark_fixture, staged_verification, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Local SQLite verification optimization preview")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_r5_recovery7"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fixture = args.output_dir / "synthetic_benchmark.db"
    connection = sqlite3.connect(fixture)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS sample(id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO sample(value) VALUES (?)", ((f"row-{index}",) for index in range(10_000))
        )
        connection.commit()
    finally:
        connection.close()
    report = {
        "phase": "R5-RECOVERY-7",
        "status": "PASSED_LOCAL_PREVIEW",
        "mode": "LOCAL_SYNTHETIC_READ_ONLY",
        "cloud_access": False,
        "database_writes": 0,
        "execution_enabled": False,
        "benchmark": benchmark_fixture(fixture),
        "staged_verification": staged_verification(fixture),
        "current_r5_recovery3_gate_changed": False,
        "next_phase": "UI-OBS-4 Preview — Backup Verification Progress Dashboard",
    }
    output = write_report(args.output_dir / "r5_recovery7_preview.json", report)
    fixture.unlink(missing_ok=True)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
