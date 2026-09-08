import json, sqlite3
c=sqlite3.connect('file:/var/lib/kalshi-bot/kalshi_phase1.db?mode=ro', uri=True)
for row in c.execute("select tbl,idx,stat from sqlite_stat1 where tbl in ('advanced_risk_decision_logs','position_sizing_decision_logs','forecasts','market_rankings','market_snapshots') order by tbl,idx"):
    print(json.dumps(row))
