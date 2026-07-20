from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1f import run_gh1f_monitor


def test_gh1f_empty_cycles_never_open_stream_or_database(tmp_path: Path) -> None:
    def empty_discovery(**_kwargs):
        return {"quoted_tickers": []}

    path = run_gh1f_monitor(
        settings=Settings(),
        output_dir=tmp_path,
        series=["KXBTC", "KXTEMPNYCH"],
        cycles=2,
        interval_seconds=0,
        max_markets_per_series=1,
        max_quoted_per_category=1,
        stream_max_seconds=0.1,
        discovery_fn=empty_discovery,
    )

    payload = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert payload["cycles_completed"] == 2
    assert payload["comparisons_triggered"] == 0
    assert payload["database_writes"] == 0
    assert payload["execution_enabled"] is False
