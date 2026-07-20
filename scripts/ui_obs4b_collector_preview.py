from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.backup_verification_collector import (
    adapt_captured_verification,
    publish_verification,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local captured backup-verification adapter")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_ui_obs4b"))
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    verification = adapt_captured_verification(fixture["sources"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {"backup_verification": verification, "execution_enabled": False}
    publication = publish_verification(snapshot, args.output_dir / "verification_snapshot.json")
    report = {
        "phase": "UI-OBS-4B",
        "status": "PASSED_LOCAL_PREVIEW" if not verification["collector_diagnostics"] else "FAILED",
        "mode": "LOCAL_CAPTURED_READ_ONLY",
        "verification": verification,
        "publication": publication,
        "next_phase": "UI-OBS-4C — Guarded Cloud Read-Only Collector Deployment",
    }
    path = args.output_dir / "ui_obs4b_collector_preview.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)
    return 0 if report["status"] == "PASSED_LOCAL_PREVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
