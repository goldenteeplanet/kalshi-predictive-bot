from pathlib import Path
import json

from kalshi_predictor.benchmarking.robustness import write_robustness_matrix


def test_pmb9_robustness_matrix_is_repeatable(tmp_path: Path) -> None:
    first = json.loads(write_robustness_matrix(tmp_path / "a").read_text())
    second = json.loads(write_robustness_matrix(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["runs"] == 18
    assert first["summary"]["replay_digest_stable_across_configurations"] is True
    assert first["execution_enabled"] is False
