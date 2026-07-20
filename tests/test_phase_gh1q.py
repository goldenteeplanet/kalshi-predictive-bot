import json
import sqlite3
from pathlib import Path

from kalshi_predictor.phase_gh1q import write_gh1q_report


def test_gh1q_attributes_selection_defects_and_missing_inputs(tmp_path: Path) -> None:
    database = tmp_path / "test.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE forecast_skip_log (id INTEGER PRIMARY KEY, model_name TEXT, ticker TEXT, "
        "skipped_at TEXT, reason TEXT, required_data TEXT, available_data TEXT)"
    )
    connection.executemany(
        "INSERT INTO forecast_skip_log VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "crypto_v2", "C1", "2026-01-01T00:00:00", "no crypto market link", "[]", "{}"),
            (2, "weather_v2", "W1", "2026-01-01T00:01:00", "no weather features", "[]", "{}"),
        ],
    )
    connection.commit()
    connection.close()

    path = write_gh1q_report(database_path=database, output_dir=tmp_path / "report", skip_limit=30)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["summary"]["skip_rows_attributed"] == 2
    assert payload["summary"]["likely_adapter_defect"] is True
    assert payload["summary"]["safe_to_repeat_refresh"] is False
    assert payload["database_writes"] == 0
    assert payload["execution_enabled"] is False
