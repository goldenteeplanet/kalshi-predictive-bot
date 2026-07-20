from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.phase_gh1p import apply_gh1p_refresh


def test_gh1p_active_writer_blocks_before_network_or_session(tmp_path: Path) -> None:
    def forbidden_session():
        raise AssertionError("session must not open")
    result = apply_gh1p_refresh(
        session_factory=forbidden_session, settings=Settings(), max_markets_per_category=1,
        writer_monitor_fn=lambda: {"safe_to_start_write": False, "current_writer_pid": 9},
    )
    assert result["status"] == "BLOCKED_ACTIVE_WRITER"
    assert result["database_writes"] == 0


def test_gh1p_eligibility_filter_keeps_only_exact_model_candidates(monkeypatch) -> None:
    import kalshi_predictor.phase_gh1p as phase

    class Session:
        pass

    monkeypatch.setattr(phase, "get_latest_crypto_link_for_ticker", lambda *_args: None)
    monkeypatch.setattr(phase, "get_latest_weather_link_for_ticker", lambda *_args: None)
    assert phase._candidate_eligibility(Session(), "crypto_v2", {"ticker": "UNLINKED"}) == (
        False, "NO_EXACT_CRYPTO_LINK"
    )
    assert phase._candidate_eligibility(Session(), "weather_v2", {"ticker": "UNLINKED"}) == (
        False, "NO_EXACT_WEATHER_LINK"
    )
