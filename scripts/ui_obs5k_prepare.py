from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.ui.app import create_app


OUTPUT = Path("reports/phase_ui_obs5k")
SNAPSHOT = OUTPUT / "ui_obs5k_20_phase_snapshot.json"
HTML = OUTPUT / "ui_obs5k_20_phase_dashboard.html"
STATIC = OUTPUT / "static"

PHASES = [
    ("R5-RECOVERY-3", "Finish backup verification and certify one bounded cycle"),
    ("R5-RECOVERY-6", "Three guarded scheduler cycles"),
    ("R5-RECOVERY-8", "Consolidated stability census"),
    ("R5-RECOVERY-9-PREVIEW", "Permanent bounded scheduler preview"),
    ("R5-RECOVERY-9", "Permanent bounded scheduler deployment"),
    ("STORAGE-CAP-1", "Backup retention model"),
    ("STORAGE-CAP-2", "Verified cold-backup archival"),
    ("PROV-14B", "Future-attribution certification retry"),
    ("PROV-14C", "Multi-cycle attribution stability census"),
    ("PROV-14D", "Guarded scheduler attribution integration"),
    ("PROV-16", "Retention, performance, and dashboard parity"),
    ("PMB-35", "Disabled exposure-guard shadow deployment"),
    ("PMB-36", "Multi-cycle shadow comparison census"),
    ("GH-1V", "Remaining liquidity windows"),
    ("GH-1X", "Liquidity and executable-edge census"),
    ("NYC-W10", "Weather live-shadow stability review"),
    ("NYC-W11", "Weather activation and rollback preview"),
    ("UI-OBS-5", "Operational dashboard summaries"),
    ("READINESS-1", "Consolidated paper-readiness recheck"),
    ("READINESS-2", "Failed-gate attribution and remediation plan"),
]


def build_phases() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    previous: str | None = None
    for index, (phase_id, label) in enumerate(PHASES):
        if index < 7:
            state = "PASSED"
            evidence = [
                {
                    "path": f"reports/certified/{phase_id.lower()}.json",
                    "status": "VERIFIED",
                    "sha256": "fixture-sha256",
                }
            ]
        elif index == 7:
            state = "RUNNING"
            evidence = []
        else:
            state = "WAITING"
            evidence = []
        rows.append(
            {
                "id": phase_id,
                "label": label,
                "state": state,
                "dependencies": [previous] if previous else [],
                "approval_required": phase_id in {"R5-RECOVERY-9", "STORAGE-CAP-2"},
                "approved": True,
                "evidence": evidence,
                "blocker": "Full integrity metadata pending" if phase_id == "PROV-14B" else "",
            }
        )
        previous = phase_id
    return rows


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "execution_enabled": False,
        "paper_enabled": False,
        "active_process": {
            "state": "RUNNING",
            "name": "SQLite full integrity verification",
            "stage": "integrity_check",
            "pid": 416265,
        },
        "backup_verification": {
            "state": "RUNNING",
            "stage": "integrity_check",
            "pid": 416265,
            "elapsed_seconds": 8640,
            "database_bytes": 23107678208,
            "read_bytes": 44048465920,
            "progress_percent_lower_bound": None,
            "estimated_remaining_seconds": None,
            "io_advanced": True,
            "stale": False,
            "integrity_status": "PENDING",
            "sha256_status": "VERIFIED",
            "path": "/mnt/kalshi-backup-02/prov14b/verified.db",
        },
        "exact_phase_roadmap": build_phases(),
    }
    SNAPSHOT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.environ["KALSHI_PROGRESS_SNAPSHOT_PATH"] = str(SNAPSHOT.resolve())
    engine = init_db(f"sqlite:///{(OUTPUT / 'ui_obs5k_render.db').resolve()}")
    client = TestClient(create_app(session_factory=get_session_factory(engine), settings=Settings()))
    response = client.get("/system/progress")
    response.raise_for_status()
    HTML.write_text(response.text, encoding="utf-8")
    STATIC.mkdir(parents=True, exist_ok=True)
    for name in ("styles.css", "app.js"):
        shutil.copyfile(Path("src/kalshi_predictor/ui/static") / name, STATIC / name)
    print(HTML)


if __name__ == "__main__":
    main()
