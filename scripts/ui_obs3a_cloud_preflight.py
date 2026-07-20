from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.cloud_deployment_preflight import write_preflight_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Certify a captured UI-OBS-3A read-only cloud preflight.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    observed = json.loads(args.capture.read_text(encoding="utf-8"))
    path = write_preflight_report(observed, args.output)
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({"status": payload["status"], "report": str(path)}, sort_keys=True))
    return 0 if payload["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
