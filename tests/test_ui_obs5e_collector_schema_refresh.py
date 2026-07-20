from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.live_status_collector import collect_live_snapshot


def runner(command: list[str], timeout: int) -> str:
    if command[:2] == ["kalshi-bot", "db-writer-monitor"]:
        return "Current writer PID: none\nSafe to start another write job: yes\n"
    if command[:2] == ["kalshi-bot", "db-locks"]:
        return "Database lock diagnostics: CLEAR\nOpen DB holders: none visible\n"
    unit = command[2]
    if unit == "kalshi-r5-bounded.service":
        return "ActiveState=inactive\nSubState=dead\nExecMainPID=0\nResult=success\n"
    if unit == "kalshi-r5-bounded.timer":
        return "ActiveState=inactive\nSubState=dead\nUnitFileState=enabled\n"
    if unit == "kalshi-r5-watcher.service":
        return "ActiveState=inactive\nUnitFileState=disabled\n"
    if unit == "kalshi-ui-status-collector.timer":
        return (
            "ActiveState=active\nSubState=waiting\nUnitFileState=enabled\n"
            "NextElapseUSecRealtime=Sun 2026-07-19 13:00:30 UTC\n"
        )
    raise AssertionError(command)


def test_exact_schema_refresh_populates_all_parity_fields(tmp_path: Path):
    roadmap = tmp_path / "roadmap.json"
    roadmap.write_text(
        json.dumps(
            {
                "phases": [
                    {
                        "number": n,
                        "phase": "PROV-14B Resume" if n == 8 else f"Phase {n}",
                        "status": "WAITING",
                        "evidence": "fixture",
                    }
                    for n in range(1, 21)
                ]
            }
        )
    )
    cert = tmp_path / "r5.json"
    cert.write_text(
        json.dumps(
            {
                "status": "PASSED",
                "gates": {"rollback_hash_verified": True},
                "rollback": {"path": "/verified/rollback"},
            }
        )
    )
    snapshot = collect_live_snapshot(
        runner=runner,
        backup_root=tmp_path,
        reports_root=tmp_path,
        roadmap_path=roadmap,
        r5_certification_path=cert,
    )
    assert snapshot["scheduler"]["service"] == "kalshi-r5-bounded.service"
    assert snapshot["scheduler"]["legacy_watcher_enabled"] is False
    assert snapshot["scheduler"]["legacy_watcher_active"] is False
    assert len(snapshot["phase_roadmap"]) == 20
    assert snapshot["r5_recovery9_certification"]["rollback_verified"] is True
    assert snapshot["prov14b"]["state"] == "WAITING"
    assert any(row["phase"] == "R5-RECOVERY-9" for row in snapshot["reports"])
