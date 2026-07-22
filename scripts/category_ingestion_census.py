#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.roadmap.category_census import (
    build_category_ingestion_census,
    write_category_ingestion_census,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only category ingestion census")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--missing-ticker-limit", type=int, default=50)
    args = parser.parse_args()
    source = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(source, dict):
        raise ValueError("input must be an object keyed by category")
    payload = build_category_ingestion_census(
        source, missing_ticker_limit=args.missing_ticker_limit
    )
    output = write_category_ingestion_census(args.output, payload)
    print(json.dumps({"output": str(output), "categories": len(payload["categories"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
