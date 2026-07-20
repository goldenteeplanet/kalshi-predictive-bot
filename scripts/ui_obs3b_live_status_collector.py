from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.ui.backup_verification_collector import collect_local_verification
from kalshi_predictor.ui.live_status_collector import collect_live_snapshot, publish_live_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the read-only UI cloud status snapshot.")
    parser.add_argument(
        "--output", type=Path, default=Path("reports/ui_obs_live/progress_snapshot.json")
    )
    parser.add_argument("--backup-root", type=Path, default=Path("/mnt/kalshi-backup"))
    parser.add_argument("--service", default="kalshi-r5-bounded.service")
    parser.add_argument("--timer", default="kalshi-r5-bounded.timer")
    parser.add_argument("--collector-timer", default="kalshi-ui-status-collector.timer")
    parser.add_argument("--legacy-service", default="kalshi-r5-watcher.service")
    parser.add_argument("--poll-interval-seconds", type=int, default=30)
    parser.add_argument(
        "--roadmap",
        type=Path,
        default=Path("reports/phase_objective_status/objective_20_phase_status_20260719.json"),
    )
    parser.add_argument(
        "--r5-certification",
        type=Path,
        default=Path("reports/phase_r5_recovery9/r5_recovery9_deployment_certification.json"),
    )
    parser.add_argument("--verification-db", type=Path)
    parser.add_argument("--integrity-output", type=Path)
    parser.add_argument("--sha256-output", type=Path)
    args = parser.parse_args()
    snapshot = collect_live_snapshot(
        backup_root=args.backup_root,
        service_name=args.service,
        timer_name=args.timer,
        collector_timer_name=args.collector_timer,
        legacy_service_name=args.legacy_service,
        roadmap_path=args.roadmap,
        r5_certification_path=args.r5_certification,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    if args.verification_db and args.integrity_output and args.sha256_output:
        snapshot["backup_verification"] = collect_local_verification(
            database_path=args.verification_db,
            integrity_output_path=args.integrity_output,
            sha256_output_path=args.sha256_output,
        )
    result = publish_live_snapshot(snapshot, args.output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
