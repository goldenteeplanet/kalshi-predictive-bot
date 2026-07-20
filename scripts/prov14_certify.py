from __future__ import annotations

import argparse
from pathlib import Path

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.phase_prov14 import write_prov14_certification_report


parser = argparse.ArgumentParser(description="Read-only PROV-14 future attribution certification")
parser.add_argument("--after-event-id", type=int, required=True)
parser.add_argument("--limit", type=int, default=200)
parser.add_argument("--output-dir", type=Path, default=Path("reports/phase_prov14"))
args = parser.parse_args()

factory = get_session_factory(init_db())
with factory() as session:
    path = write_prov14_certification_report(
        session,
        after_event_id=args.after_event_id,
        expected_models=("crypto_v2", "weather_v2"),
        output_dir=args.output_dir,
        limit=args.limit,
    )
print(path)
