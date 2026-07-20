from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.r5_recovery9 import certify_preview, write_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify the inert bounded R5 scheduler preview")
    parser.add_argument(
        "--service",
        type=Path,
        default=Path("deploy/systemd/kalshi-r5-bounded.service.preview"),
    )
    parser.add_argument(
        "--timer", type=Path, default=Path("deploy/systemd/kalshi-r5-bounded.timer.preview")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_r5_recovery9/r5_recovery9_preview.json"),
    )
    args = parser.parse_args()
    report = certify_preview(args.service, args.timer)
    print(write_report(args.output, report))
    return 0 if report["status"] == "PASSED_LOCAL_PREVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
