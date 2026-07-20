import copy
import json

from kalshi_predictor.benchmarking.portfolio import write_portfolio_benchmark
from kalshi_predictor.benchmarking.provenance_portfolio import (
    build_provenance_aware_portfolio_report,
    verify_portfolio_provenance_chain,
    write_provenance_aware_portfolio_benchmark,
)


def test_pmb10_is_deterministic_and_covers_all_market_categories(tmp_path):
    first = json.loads(write_provenance_aware_portfolio_benchmark(tmp_path / "a").read_text())
    second = json.loads(write_provenance_aware_portfolio_benchmark(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["chain_valid"] is True
    assert first["summary"]["all_categories_covered"] is True
    assert set(first["summary"]["category_coverage"]) == {"crypto", "weather", "sports"}
    assert all(row["provenance_digest"] for row in first["trade_logs"])
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb10_detects_chain_tampering(tmp_path):
    base = json.loads(write_portfolio_benchmark(tmp_path / "base").read_text())
    report = build_provenance_aware_portfolio_report(base)
    chain = copy.deepcopy(report["decision_provenance"])
    chain[0]["model_version"] = "tampered"
    result = verify_portfolio_provenance_chain(chain)
    assert result["valid"] is False
    assert result["failures"][0] == {"index": 0, "failure": "DIGEST_MISMATCH"}


def test_pmb10_rejects_decision_without_exact_attribution(tmp_path):
    base = json.loads(write_portfolio_benchmark(tmp_path / "base").read_text())
    base["allocation_decisions"][0]["ticker"] = "UNKNOWN"
    try:
        build_provenance_aware_portfolio_report(base)
    except ValueError as exc:
        assert "exact synthetic attribution missing" in str(exc)
    else:
        raise AssertionError("missing attribution was accepted")
