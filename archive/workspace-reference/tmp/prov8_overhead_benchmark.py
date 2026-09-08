import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import kalshi_predictor.memory.capture as memory_capture
from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.data.schema import Forecast, Market, RuntimeProvenanceEvent

memory_capture.capture_forecast_created = lambda *args, **kwargs: None
now = datetime.now(timezone.utc)

def run(enabled: bool, count: int = 200) -> float:
    path = Path(f'/tmp/prov8_overhead_{enabled}.db')
    path.unlink(missing_ok=True)
    engine = create_engine(f'sqlite:///{path}')
    for table in (Market.__table__, Forecast.__table__, RuntimeProvenanceEvent.__table__):
        table.create(engine, checkfirst=True)
    with Session(engine) as session:
        session.add(Market(ticker='PROV8-BENCH', raw_json='{}', first_seen_at=now,
                           last_seen_at=now))
        session.flush()
        start = time.perf_counter()
        for index in range(count):
            insert_forecast(session, {
                'ticker': 'PROV8-BENCH', 'forecasted_at': now,
                'model_name': 'crypto_v2', 'yes_probability': Decimal('0.55'),
                'feature_json': {'crypto_feature_id': index + 1},
            }, attribution_enabled=enabled)
        session.commit()
        elapsed = time.perf_counter() - start
    path.unlink()
    return elapsed

disabled = run(False)
enabled = run(True)
print(json.dumps({
    'rows': 200, 'disabled_seconds': disabled, 'enabled_seconds': enabled,
    'overhead_seconds': enabled - disabled,
    'overhead_ms_per_row': ((enabled - disabled) / 200) * 1000,
    'ratio': enabled / disabled if disabled else None,
}, sort_keys=True))
