from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import ui_obs5m_prepare as preview

from kalshi_predictor.ui.progress_history import history_path_for, record_progress_snapshot

preview.OUTPUT = Path("reports/phase_ui_obs5o")
preview.SNAPSHOT = preview.OUTPUT / "ui_obs5o_snapshot.json"
preview.HTML = preview.OUTPUT / "ui_obs5o_dashboard.html"


def main() -> None:
    preview.main()
    current = json.loads(preview.SNAPSHOT.read_text(encoding="utf-8"))
    history = history_path_for(preview.SNAPSHOT)
    history.unlink(missing_ok=True)
    now = datetime.now(UTC)
    states = [
        ("WAITING", "WAITING", "WAITING", "WAITING"),
        ("PASSED", "RUNNING", "WAITING", "WAITING"),
        ("PASSED", "PASSED", "PASSED", "PASSED"),
    ]
    for index, gate_states in enumerate(states):
        captured = now - timedelta(seconds=120 - index * 60)
        snapshot = deepcopy(current)
        timestamp = captured.isoformat().replace("+00:00", "Z")
        snapshot["generated_at"] = timestamp
        pipeline = snapshot["prov14b_certification_pipeline"]
        pipeline["captured_at"] = timestamp
        for gate_id, state in zip(("R2A", "R2B", "R2C", "R2D"), gate_states, strict=True):
            gate = pipeline["gates"][gate_id]
            gate["state"] = state
            gate["generated_at"] = timestamp
        if index == 0:
            pipeline["gates"]["R2C"]["generated_at"] = (
                captured - timedelta(seconds=3601)
            ).isoformat()
        record_progress_snapshot(snapshot, history)
    time.sleep(2.1)
    preview.main()


if __name__ == "__main__":
    main()
