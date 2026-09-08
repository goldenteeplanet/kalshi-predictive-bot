from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, make_sqlite_read_only_engine
from kalshi_predictor.ingest.cycle_handoff import write_committed_cycle_artifact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    factory = get_session_factory(make_sqlite_read_only_engine(f"sqlite:///{args.source_db}"))
    with factory() as source:
        payload = write_committed_cycle_artifact(source, output_path=args.output)
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
