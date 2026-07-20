"""Consume newly certified NYC-W4 windows into the disabled W8 shadow census."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kalshi_predictor.phase_nyc_w7 import write_shadow_runtime_report
from kalshi_predictor.phase_nyc_w8 import write_nyc_w8_report
from kalshi_predictor.utils.time import parse_datetime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_nyc_w8"))
    parser.add_argument("--max-adjustment", type=Decimal, default=Decimal("0.10"))
    args = parser.parse_args()
    state_path = args.output_dir / "cadence_state.json"
    state = _load_or_start(state_path)
    started_at = parse_datetime(state["started_at"])
    consumed = set(state.get("consumed_source_reports", []))

    for source in sorted(args.reports_dir.glob(
        "phase_nyc_w4*/nyc_w4_observation_feature_integration_preview.json"
    )):
        source_key = str(source.resolve())
        if source_key in consumed:
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        generated_at = parse_datetime(payload.get("generated_at"))
        if generated_at is None or generated_at <= started_at:
            continue
        targets = sorted({
            str(row.get("target_utc_time") or "") for row in payload.get("rows", [])
            if row.get("preview_passed") and row.get("target_utc_time")
        })
        if not targets:
            continue
        slug = targets[0].replace(":", "").replace("+", "p").replace("-", "")
        write_shadow_runtime_report(
            reports_dir=args.reports_dir,
            output_dir=args.reports_dir / f"phase_nyc_w7_live_{slug}",
            max_adjustment=args.max_adjustment,
            source_paths=[source],
        )
        consumed.add(source_key)

    state["consumed_source_reports"] = sorted(consumed)
    state["last_run_at"] = datetime.now(timezone.utc).isoformat()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    print(write_nyc_w8_report(reports_dir=args.reports_dir, output_dir=args.output_dir))


def _load_or_start(path: Path) -> dict[str, object]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "consumed_source_reports": [],
        "mode": "READ_ONLY_DISABLED_FEATURE_FLAG",
    }


if __name__ == "__main__":
    main()
