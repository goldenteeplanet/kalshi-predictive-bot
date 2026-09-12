"""Explicit original-backed crypto_v3 average research route, never paper."""

import argparse
import json
from pathlib import Path

from kalshi_predictor.crypto.research_shadow import LIMIT, initialize_journal
from kalshi_predictor.forecasting.crypto_research_router import (
    crypto_average_research_status,
    record_crypto_average_research,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "record", "status"))
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.action == "initialize":
        initialize_journal(args.journal)
        result = {"status": "NEW_RESEARCH_JOURNAL", "execution_authority": False}
    elif args.action == "record":
        if (
            args.request is None
            or args.request.is_symlink()
            or not 0 < args.request.stat().st_size <= LIMIT
        ):
            parser.error("bounded original --request required")
        result = record_crypto_average_research(
            journal=args.journal, request_raw=args.request.read_bytes()
        )
    else:
        result = crypto_average_research_status(journal=args.journal)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
