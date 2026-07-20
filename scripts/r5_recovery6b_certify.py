from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.r5_recovery6b import certify_quarantine, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Local R5 quarantine/rollback certification")
    parser.add_argument("--scenarios", type=Path, required=True)
    parser.add_argument("--rollback-bundle", required=True)
    parser.add_argument("--backup-path", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_r5_recovery6b/r5_recovery6b_certification_preview.json"),
    )
    args = parser.parse_args()
    scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))["scenarios"]
    report = certify_quarantine(
        scenarios,
        rollback_bundle=args.rollback_bundle,
        backup_path=args.backup_path,
    )
    print(write_report(args.output, report))
    return 0 if report["status"] == "PASSED_LOCAL_PREVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
