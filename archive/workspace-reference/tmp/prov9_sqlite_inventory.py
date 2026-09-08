import json
import sqlite3

db = "/var/lib/kalshi-bot/kalshi_phase1.db"
tables = [
    "forecasts", "market_rankings", "market_snapshots", "markets",
    "crypto_market_links", "weather_market_links", "economic_market_links",
    "sports_market_links", "crypto_prices", "crypto_features",
    "weather_forecasts", "weather_features", "economic_events", "economic_features",
    "position_sizing_decision_logs", "advanced_risk_decision_logs",
]
connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
result = {}
for table in tables:
    try:
        result[table] = {
            "indexes": [dict(zip(("seq", "name", "unique", "origin", "partial"), row))
                        for row in connection.execute(f"pragma index_list('{table}')")],
        }
        for item in result[table]["indexes"]:
            item["columns"] = [row[2] for row in connection.execute(
                f"pragma index_info('{item['name']}')"
            )]
    except sqlite3.Error as exc:
        result[table] = {"error": str(exc)}
print(json.dumps(result, indent=2, sort_keys=True))
