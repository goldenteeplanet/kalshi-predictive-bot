import hashlib
import json
import sqlite3

c = sqlite3.connect('/var/lib/kalshi-bot/kalshi_phase1.db')
c.row_factory = sqlite3.Row
rows = list(c.execute('SELECT * FROM runtime_provenance_events ORDER BY id'))
errors = []
for row in rows:
    raw = json.loads(row['raw_json'])
    digest = hashlib.sha256(json.dumps(
        raw, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    if digest != row['provenance_digest']:
        errors.append(f"digest:{row['id']}")
    if c.execute('SELECT COUNT(*) FROM forecasts WHERE id=?',
                 (row['forecast_id'],)).fetchone()[0] != 1:
        errors.append(f"forecast:{row['id']}")
    if row['ranking_id'] is not None and c.execute(
        'SELECT COUNT(*) FROM market_rankings WHERE id=?', (row['ranking_id'],)
    ).fetchone()[0] != 1:
        errors.append(f"ranking:{row['id']}")
print(json.dumps({
    'events': len(rows), 'errors': errors, 'valid': not errors,
    'forecast_events': sum(row['stage'] == 'FORECAST_CREATED' for row in rows),
    'ranking_events': sum(row['stage'] == 'RANKING_CREATED' for row in rows),
}, sort_keys=True))
