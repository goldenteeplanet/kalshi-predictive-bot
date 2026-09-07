from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, make_engine, make_sqlite_read_only_engine
from phase4m_research_handoff import _load_isolated_research_models


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--research-db", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    research_engine = make_engine(f"sqlite:///{args.research_db}")
    _load_isolated_research_models(research_engine)
    from kalshi_predictor.phase4cd.prospective import watch_canonical_settlements

    research_factory = get_session_factory(research_engine)
    source_factory = get_session_factory(
        make_sqlite_read_only_engine(f"sqlite:///{args.source_db}")
    )
    with research_factory() as research, source_factory() as source:
        payload = watch_canonical_settlements(research, source, limit=args.limit)
    print(json.dumps(payload, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
