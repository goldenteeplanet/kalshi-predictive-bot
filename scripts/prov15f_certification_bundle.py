from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.provenance.bundle import write_offline_certification_bundle
from kalshi_predictor.utils.time import parse_datetime

parser = argparse.ArgumentParser(description="Build deterministic PROV-15F bundle")
parser.add_argument("--root", type=Path, default=Path("."))
parser.add_argument("--output-dir", type=Path, required=True)
parser.add_argument("--generated-at", required=True)
args = parser.parse_args()

generated_at = parse_datetime(args.generated_at)
if generated_at is None:
    raise SystemExit("--generated-at must be an ISO-8601 timestamp")
artifacts = {
    "PROV-15": args.root / "reports/phase_prov15/prov15_attribution_regression_golden.json",
    "PROV-15B": args.root / "reports/phase_prov15b/prov15b_runtime_export_golden_comparison.json",
    "PROV-15C": args.root / "reports/phase_prov15c/prov15c_export_schema_compatibility_matrix.json",
    "PROV-15D": args.root / "reports/phase_prov15d/prov15d_offline_provenance_failure_triage.json",
    "PROV-15E": args.root / "reports/phase_prov15e/prov15e_offline_remediation_before_after.json",
}
bundle, manifest = write_offline_certification_bundle(
    artifacts, generated_at=generated_at, root=args.root, output_dir=args.output_dir
)
print(bundle)
print(manifest)
