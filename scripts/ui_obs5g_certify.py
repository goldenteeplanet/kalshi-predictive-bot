from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.live_poll_stability import certify_live_poll_stability


paths = [
    Path("reports/phase_ui_obs5g/live_snapshot_2.json"),
    Path("reports/phase_ui_obs5g/live_snapshot_3.json"),
    Path("reports/phase_ui_obs5g/live_snapshot_4.json"),
]
report = certify_live_poll_stability([json.loads(path.read_text(encoding="utf-8")) for path in paths])
report["source_snapshots"] = [str(path) for path in paths]
output = Path("reports/phase_ui_obs5g/ui_obs5g_multi_poll_live_stability_census.json")
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
