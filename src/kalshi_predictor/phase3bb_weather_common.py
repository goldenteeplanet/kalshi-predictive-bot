from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    CloudBootstrapTarget,
    RemoteProbeResult,
)


def check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        pass


def stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


def target_payload(target: CloudBootstrapTarget) -> dict[str, str]:
    return {
        "ssh_target": target.ssh_target,
        "identity_file": target.identity_file,
        "app_path": target.app_path,
        "env_path": target.env_path,
        "db_path": target.db_path,
        "reports_path": target.reports_path,
    }


def write_rows_csv(
    path: Path,
    rows: list[dict[str, Any]],
    fieldnames: list[str] | None = None,
) -> None:
    if fieldnames is None:
        keys: list[str] = []
        for row in rows:
            for key in row:
                if key not in keys:
                    keys.append(key)
        fieldnames = keys or ["empty"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "ok",
        "exit_code",
        "duration_seconds",
        "timed_out",
        "stdout_excerpt",
        "stderr_excerpt",
    ]
    write_rows_csv(
        path,
        [{name: row.get(name) for name in fieldnames} for row in rows],
        fieldnames=fieldnames,
    )
