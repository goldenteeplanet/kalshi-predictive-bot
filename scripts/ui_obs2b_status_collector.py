from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.ui.status_collector import write_collector_resilience_preview


parser = argparse.ArgumentParser(description="Run local UI-OBS-2B collector resilience preview")
parser.add_argument("--fixture", type=Path, default=Path("tests/fixtures/ui_obs2b/collector_fixture.json"))
parser.add_argument("--output-dir", type=Path, default=Path("reports/ui_obs2b"))
args = parser.parse_args()
print(write_collector_resilience_preview(args.fixture, args.output_dir))
