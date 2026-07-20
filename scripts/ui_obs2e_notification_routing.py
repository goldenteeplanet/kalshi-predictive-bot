from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.ui.notification_routing import write_notification_routing_preview


parser = argparse.ArgumentParser(description="Run local UI-OBS-2E notification routing preview")
parser.add_argument("--incidents", type=Path, default=Path("reports/ui_obs2d/ui_obs2d_incident_resolution_preview.json"))
parser.add_argument("--policy", type=Path, default=Path("tests/fixtures/ui_obs2e/policy.json"))
parser.add_argument("--ledger", type=Path, default=Path("tests/fixtures/ui_obs2e/prior_ledger.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2e"))
args = parser.parse_args()
print(write_notification_routing_preview(args.incidents, args.policy, args.ledger, args.output_dir))
