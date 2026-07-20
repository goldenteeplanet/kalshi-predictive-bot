from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.phase_nyc_w11 import write_nyc_w11_preview


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the inert NYC-W11 activation preview")
    parser.add_argument(
        "--w10",
        type=Path,
        default=Path("reports/phase_nyc_w10/nyc_w10_live_shadow_stability_review.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_nyc_w11/nyc_w11_activation_preview.json"),
    )
    args = parser.parse_args()
    path = write_nyc_w11_preview(args.w10, args.output)
    report = json.loads(path.read_text(encoding="utf-8"))
    print(path)
    return 0 if report["status"] == "PASSED_LOCAL_PREVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
