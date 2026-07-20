from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.readiness2b import build_remediation_roadmap, write_remediation_roadmap

parser = argparse.ArgumentParser(description="Build the latest read-only remediation roadmap.")
parser.add_argument("--objective", type=Path, default=Path("reports/phase_objective_status/objective_20_phase_status_20260719.json"))
parser.add_argument("--readiness", type=Path, default=Path("reports/phase_readiness2/readiness2_failed_gate_attribution_fresh_20260719.json"))
parser.add_argument("--liquidity", type=Path, default=Path("reports/phase_gh1x/gh1x_liquidity_edge_risk_census.json"))
parser.add_argument("--output", type=Path, default=Path("reports/phase_readiness2b/readiness2b_remediation_roadmap.json"))
args = parser.parse_args()
print(write_remediation_roadmap(args.output, build_remediation_roadmap(args.objective, args.readiness, args.liquidity)))
