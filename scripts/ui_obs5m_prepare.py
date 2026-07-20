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

OUTPUT = Path("reports/phase_ui_obs5m")
SNAPSHOT = OUTPUT / "ui_obs5m_snapshot.json"
HTML = OUTPUT / "ui_obs5m_dashboard.html"


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload = {
        "generated_at": now,
        "execution_enabled": False,
        "paper_enabled": False,
        "active_process": {
            "state": "RUNNING",
            "name": "PROV-14B-R2 backup certification",
            "stage": "integrity_check",
            "pid": 432831,
        },
        "prov14b_certification_pipeline": {
            "captured_at": now,
            "current_stage": "integrity_check",
            "backup_stages": {
                "backup_copy": {"state": "PASSED", "evidence": "23.1 GB"},
                "quick_check": {"state": "PASSED", "evidence": "ok"},
                "sha256": {"state": "PASSED", "evidence": "4a1e8a8e…b203"},
                "integrity_check": {"state": "RUNNING", "detail": "Full check active"},
            },
            "gates": {
                gate: {
                    "id": gate,
                    "state": "PASSED",
                    "report_sha256": character * 64,
                    "failed_count": 0,
                    "generated_at": now,
                    "artifact_id": f"prov14b-{gate.lower()}-preview",
                    "runtime_certified": False,
                    "detail": "Local deterministic preview passed",
                    "evidence_details": [
                        {"label": "Report", "value": f"reports/phase_prov14b_{gate.lower()}"},
                        {"label": "Failures", "value": "0"},
                    ],
                }
                for gate, character in zip(("R2A", "R2B", "R2C", "R2D"), "abcd", strict=True)
            },
        },
    }
    SNAPSHOT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.environ["KALSHI_PROGRESS_SNAPSHOT_PATH"] = str(SNAPSHOT.resolve())
    engine = init_db(f"sqlite:///{(OUTPUT / 'ui_obs5m_render.db').resolve()}")
    client = TestClient(
        create_app(session_factory=get_session_factory(engine), settings=Settings())
    )
    response = client.get("/system/progress")
    response.raise_for_status()
    HTML.write_text(response.text, encoding="utf-8")
    static = OUTPUT / "static"
    static.mkdir(exist_ok=True)
    for name in ("styles.css", "app.js"):
        shutil.copyfile(Path("src/kalshi_predictor/ui/static") / name, static / name)
    print(HTML)


if __name__ == "__main__":
    main()
