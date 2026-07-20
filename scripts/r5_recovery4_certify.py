from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.r5_recovery4 import build_certification_report, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Local deterministic R5 stability certification")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--cycles", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_r5_recovery4/r5_recovery4_certification.json"),
    )
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    cycle_payload = json.loads(args.cycles.read_text(encoding="utf-8"))
    cycles = cycle_payload["cycles"] if isinstance(cycle_payload, dict) else cycle_payload
    report = build_certification_report(baseline=baseline, cycles=cycles)
    print(write_report(args.output, report))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
