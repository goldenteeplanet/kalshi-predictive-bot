from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.ui.deployment_harness_certification import certify_fail_closed_harness


report = certify_fail_closed_harness(
    Path("reports/phase_ui_obs5f_retry/deploy_ui_obs5f_retry.sh"),
    Path("deploy/systemd/kalshi-ui-status-collector.service.ui-obs5fa.preview"),
)
output = Path("reports/phase_ui_obs5fb/ui_obs5fb_deployment_harness_certification.json")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(output)
