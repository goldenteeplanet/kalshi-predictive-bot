from __future__ import annotations

import argparse
import json
from pathlib import Path

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.backend import database_url_from_settings
from kalshi_predictor.data.db import get_session_factory, make_engine
from kalshi_predictor.paper.invariant_monitor import (
    write_activated_trade_invariant_status,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/paper_activation/invariant_status.json"),
    )
    args = parser.parse_args()
    settings = get_settings()
    engine = make_engine(database_url_from_settings(settings))
    try:
        with get_session_factory(engine)() as session:
            payload = write_activated_trade_invariant_status(
                session, output_path=args.output
            )
            session.rollback()
    finally:
        engine.dispose()
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
