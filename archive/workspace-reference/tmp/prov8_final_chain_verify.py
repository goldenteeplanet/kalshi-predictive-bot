import hashlib
import json
import sqlite3

c = sqlite3.connect('/var/lib/kalshi-bot/kalshi_phase1.db')
c.row_factory = sqlite3.Row
rows = list(c.execute('SELECT * FROM runtime_provenance_events ORDER BY id'))
errors = []
previous = {}
for row in rows:
    raw = json.loads(row['raw_json'])
    digest = hashlib.sha256(json.dumps(
        raw, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    if digest != row['provenance_digest']:
        errors.append(f"digest:{row['id']}")
    expected_previous = previous.get(row['forecast_id'], 'GENESIS')
    if row['previous_digest'] != expected_previous:
        errors.append(f"chain:{row['id']}")
    previous[row['forecast_id']] = row['provenance_digest']
    if c.execute('SELECT COUNT(*) FROM forecasts WHERE id=?',
                 (row['forecast_id'],)).fetchone()[0] != 1:
        errors.append(f"forecast:{row['id']}")
    if row['ranking_id'] is not None and c.execute(
        'SELECT COUNT(*) FROM market_rankings WHERE id=?', (row['ranking_id'],)
    ).fetchone()[0] != 1:
        errors.append(f"ranking:{row['id']}")
duplicates = c.execute('SELECT COUNT(*)-COUNT(DISTINCT event_key) FROM runtime_provenance_events').fetchone()[0]
if duplicates:
    errors.append(f'duplicate_event_keys:{duplicates}')
print(json.dumps({
    'events': len(rows), 'forecast_events': sum(r['stage']=='FORECAST_CREATED' for r in rows),
    'ranking_events': sum(r['stage']=='RANKING_CREATED' for r in rows),
    'models': {r['model_name'] for r in rows}, 'errors': errors, 'valid': not errors,
}, sort_keys=True, default=sorted))
