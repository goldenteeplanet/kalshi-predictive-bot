from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from kalshi_predictor.readiness2 import build_readiness2_preview, write_readiness2_preview


def main() -> int:
    parser = argparse.ArgumentParser(description="Attribute failed paper-readiness gates")
    parser.add_argument(
        "--blockers", type=Path, default=Path("reports/phase3bb_r8/category_blockers.csv")
    )
    parser.add_argument(
        "--summary", type=Path, default=Path("reports/phase3bb_r8/EXECUTIVE_SUMMARY.md")
    )
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_readiness2/readiness2_failed_gate_attribution.json"),
    )
    args = parser.parse_args()
    report = build_readiness2_preview(args.blockers, args.summary, as_of=args.as_of)
    print(write_readiness2_preview(args.output, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
