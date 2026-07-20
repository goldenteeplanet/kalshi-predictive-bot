from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from kalshi_predictor.ui.live_roadmap_status import build_live_roadmap_status


source = Path("reports/phase_ui_obs5g/live_snapshot_4.json")
payload = json.loads(source.read_text(encoding="utf-8"))
reference = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
report = build_live_roadmap_status(payload, reference_time=reference)
report["alerts"] = payload.get("alerts", [])
report["phase"] = "UI-OBS-5H"
report["source_snapshot"] = str(source)
output = Path("reports/phase_ui_obs5h/ui_obs5h_captured_live_render_input.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
