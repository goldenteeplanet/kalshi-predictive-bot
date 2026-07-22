#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.roadmap.deployment_preflight import (
    build_deployment_preflight,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only exact-SHA deployment manifest")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--rollback-sha", required=True)
    parser.add_argument("--environment-file", type=Path, required=True)
    parser.add_argument("--backup-database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_deployment_preflight(
        repo=args.repo,
        target_sha=args.target_sha,
        rollback_sha=args.rollback_sha,
        environment_file=args.environment_file,
        backup_database=args.backup_database,
    )
    write_manifest(args.output, manifest)
    print(json.dumps({"output": str(args.output), "manifest_sha256": manifest["manifest_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
