from __future__ import annotations
import argparse,json
from pathlib import Path
from kalshi_predictor.ui.regression_scenarios import SCENARIOS,regression_snapshot
parser=argparse.ArgumentParser(); parser.add_argument("scenario",choices=SCENARIOS); parser.add_argument("--output",type=Path,default=Path("reports/ui_obs2p/active_snapshot.json")); args=parser.parse_args(); args.output.parent.mkdir(parents=True,exist_ok=True); temporary=args.output.with_suffix(".json.tmp"); temporary.write_text(json.dumps(regression_snapshot(args.scenario),indent=2,sort_keys=True)+"\n"); temporary.replace(args.output); print(args.output)
