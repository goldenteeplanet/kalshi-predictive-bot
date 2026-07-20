from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.ui.notification_receipt_audit import write_notification_receipt_audit


parser = argparse.ArgumentParser(description="Run local UI-OBS-2G receipt reconciliation")
parser.add_argument("--routing", type=Path, default=Path("reports/ui_obs2e/ui_obs2e_notification_routing_preview.json"))
parser.add_argument("--delivery", type=Path, default=Path("reports/ui_obs2f/ui_obs2f_local_delivery_simulator.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2g"))
args = parser.parse_args()
print(write_notification_receipt_audit(args.routing, args.delivery, args.output_dir))
