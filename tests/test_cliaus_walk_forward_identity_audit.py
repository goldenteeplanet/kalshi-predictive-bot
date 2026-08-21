import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "cliaus_walk_forward_identity_audit.py"
_SPEC = importlib.util.spec_from_file_location("cliaus_walk_forward_identity_audit", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_scoring_is_prequential_and_covers_every_threshold() -> None:
    samples = [
        {
            "month": f"2025-{i + 1:02d}",
            "predicted_total_inches": "1.2",
            "actual_total_inches": "1.4",
        }
        for i in range(12)
    ]
    result = _MODULE.score_samples(samples)
    assert result["sample_count"] == 12
    assert result["scored_binary_rows"] == 84
    assert set(result["by_threshold"]) == {str(i) for i in range(1, 8)}


def test_identity_requires_exact_station_threshold_and_settlement_language() -> None:
    candidate = {"ticker": "KXRAINAUSM-26AUG-2", "executable_ev": "0.1", "ranking_id": 1}
    market = {
        "ticker": candidate["ticker"],
        "event_ticker": "KXRAINAUSM-26AUG",
        "status": "active",
        "rules_primary": (
            "If total precipitation at CLIAUS in Austin in Aug 2026 is "
            "strictly greater than 2 inches, resolves Yes."
        ),
        "rules_secondary": "CLIAUS station source.",
    }
    assert _MODULE.audit_market(candidate, market)["passed"] is True
