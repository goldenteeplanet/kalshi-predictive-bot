from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.r5_recovery6a import run_census, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Local R5 three-cycle evidence certification")
    parser.add_argument("--cycles", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_r5_recovery6a/r5_recovery6a_certification_preview.json"),
    )
    args = parser.parse_args()
    cycles = json.loads(args.cycles.read_text(encoding="utf-8"))["cycles"]
    previous = (
        json.loads(args.resume_from.read_text(encoding="utf-8")) if args.resume_from else None
    )
    report = run_census(cycles, previous_report=previous)
    print(write_report(args.output, report))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
