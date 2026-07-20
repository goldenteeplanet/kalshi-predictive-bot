from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.phase_nyc_w10 import write_nyc_w10_review


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the read-only NYC-W10 stability review.")
    parser.add_argument(
        "--w8-report",
        type=Path,
        default=Path("reports/phase_nyc_w8/nyc_w8_live_shadow_drift_certification.json"),
    )
    parser.add_argument(
        "--w9-report",
        type=Path,
        default=Path("reports/phase_nyc_w9/nyc_w9_live_window_feed.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_nyc_w10"))
    parser.add_argument("--w8-timer-active", action="store_true")
    parser.add_argument("--w9-timer-active", action="store_true")
    parser.add_argument("--failed-runs", type=int, default=0)
    args = parser.parse_args()

    path = write_nyc_w10_review(
        w8_report=args.w8_report,
        w9_report=args.w9_report,
        output_dir=args.output_dir,
        operations_evidence={
            "w8_timer_active": args.w8_timer_active,
            "w9_timer_active": args.w9_timer_active,
            "failed_runs": args.failed_runs,
        },
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
