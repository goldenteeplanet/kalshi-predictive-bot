from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.storage_capacity import build_capacity_plan, write_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only backup capacity plan")
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/phase_storage_cap1/storage_cap1_capacity_plan.json"),
    )
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    plan = build_capacity_plan(**inventory)
    print(write_plan(args.output, plan))
    return 0 if plan["status"] == "PASSED_LOCAL_PREVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
