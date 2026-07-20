import json
import sqlite3
from pathlib import Path

from kalshi_predictor.phase_gh1j import write_gh1j_report


def test_gh1j_identifies_missing_snapshot_as_first_break(tmp_path: Path) -> None:
    db = tmp_path / "audit.db"
    connection = sqlite3.connect(db)
    for table, timestamp in (("market_snapshots", "captured_at"), ("market_rankings", "ranked_at"),
                             ("market_opportunities", "detected_at"), ("advanced_risk_decisions", "decision_timestamp")):
        connection.execute(f"CREATE TABLE {table} (ticker TEXT, {timestamp} TEXT)")
    connection.close()
    source = tmp_path / "gh1i.json"
    source.write_text(json.dumps({"two_sided_books": [{"ticker": "KXBTC-TEST", "category": "crypto",
        "calibration": {"ranking_advance": True, "risk_executable_advance": True}}]}), encoding="utf-8")
    path = write_gh1j_report(gh1i_report=source, database_path=db, output_dir=tmp_path / "out")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["first_break_counts"] == {"LOCAL_SNAPSHOT_MISSING": 1}
    assert payload["database_writes"] == 0
