from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.ui.cloud_status_adapter import write_cloud_status_adapter_preview


parser = argparse.ArgumentParser(description="Run local UI-OBS-2 cloud status adapter shadow")
parser.add_argument("--bundle", type=Path, default=Path("tests/fixtures/ui_obs2/cloud_status_bundle.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2"))
args = parser.parse_args()
print(write_cloud_status_adapter_preview(args.bundle, args.output_dir))
