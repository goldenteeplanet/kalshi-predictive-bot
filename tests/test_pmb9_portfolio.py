import json
from pathlib import Path

from kalshi_predictor.benchmarking.portfolio import write_portfolio_benchmark


def test_pmb9_portfolio_is_deterministic_and_limit_safe(tmp_path: Path) -> None:
    first = json.loads(write_portfolio_benchmark(tmp_path / "a").read_text())
    second = json.loads(write_portfolio_benchmark(tmp_path / "b").read_text())
    assert first["deterministic_digest"] == second["deterministic_digest"]
    assert first["episode"]["market_count"] == 3
    assert first["summary"]["all_exposure_limits_respected"] is True
    assert set(first["episode"]["categories"].values()) == {"crypto", "weather", "sports"}
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False
