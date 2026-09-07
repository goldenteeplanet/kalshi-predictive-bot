"""Sole-writer settlement sync with validated outcome-blind exact hints."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.ingest.settlement_hints import validate_hint_artifact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hint-artifact", type=Path, required=True)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=10)
    args = parser.parse_args()
    tickers = validate_hint_artifact(args.hint_artifact, now=datetime.now(UTC))
    telemetry: list[dict[str, object]] = []
    count = sync_settlements(
        lookback_days=args.lookback_days,
        limit=args.limit,
        max_pages=args.max_pages,
        exact_recovery_tickers=tickers,
        telemetry_callback=telemetry.append,
    )
    print(json.dumps({"settlements_synced": count, "telemetry": telemetry[-1]}))


if __name__ == "__main__":
    main()
