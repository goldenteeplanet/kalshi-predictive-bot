"""Offline original-bound average-model research writer and dashboard status CLI."""

import argparse
import json
from pathlib import Path

from kalshi_predictor.crypto.research_shadow import (
    LIMIT,
    append_decision,
    initialize_journal,
    journal_status,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("initialize", "append", "status"))
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.action == "initialize":
        initialize_journal(args.journal)
        result = {"status": "NEW_RESEARCH_JOURNAL_CREATED", "execution_authority": False}
    elif args.action == "append":
        if args.request is None or not 0 < args.request.stat().st_size <= LIMIT:
            parser.error("append requires a bounded --request JSON file")
        result = append_decision(args.journal, args.request.read_bytes())
    else:
        result = journal_status(args.journal)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
