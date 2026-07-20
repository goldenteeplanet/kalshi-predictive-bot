from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.dashboard_deployment_preview import certify_dashboard_deployment_preview


report = certify_dashboard_deployment_preview(
    Path.cwd(), Path("deploy/systemd/kalshi-ui.service.ui-obs5ia.preview"),
    Path("reports/phase_ui_obs5i/deploy_ui_obs5i.sh"),
)
output = Path("reports/phase_ui_obs5ia/ui_obs5ia_dashboard_deployment_preview.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
