from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.collector_invocation_preview import certify_invocation_preview


preview = Path("deploy/systemd/kalshi-ui-status-collector.service.ui-obs5fa.preview")
report = certify_invocation_preview(preview)
output = Path("reports/phase_ui_obs5fa/ui_obs5fa_exact_collector_invocation_preview.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
