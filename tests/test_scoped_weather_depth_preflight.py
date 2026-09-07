from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import scripts.scoped_weather_depth_preflight as preflight


def _gate_row(ticker, *, eligible=True):
    return {
        "ticker": ticker,
        "executable_book": eligible,
        "buy_price_matches_ranking": True,
        "sufficient_buy_side_size": True,
        "first_blocker": "PAPER_READY",
    }


def test_dynamic_scope_selects_only_currently_eligible_rows() -> None:
    payload = {"weather_rows": [_gate_row("READY"), _gate_row("STALE", eligible=False)]}
    assert tuple(preflight.select_scoped_gate_rows(payload, None)) == ("READY",)


def test_explicit_scope_remains_strict_and_fail_closed() -> None:
    payload = {"weather_rows": [_gate_row("READY"), _gate_row("STALE", eligible=False)]}
    try:
        preflight.select_scoped_gate_rows(payload, ["READY", "STALE"])
    except RuntimeError as error:
        assert "no longer eligible" in str(error)
    else:
        raise AssertionError("ineligible explicit ticker was accepted")


def test_dynamic_scope_refuses_empty_eligible_set() -> None:
    try:
        preflight.select_scoped_gate_rows(
            {"weather_rows": [_gate_row("STALE", eligible=False)]}, None
        )
    except RuntimeError as error:
        assert "no currently eligible" in str(error)
    else:
        raise AssertionError("empty dynamic scope was accepted")


def test_refreshes_expired_cache_only_when_history_is_unchanged() -> None:
    cutoff = datetime(2026, 8, 28, 5, tzinfo=UTC)
    refreshed_at = datetime(2026, 8, 28, 16, tzinfo=UTC)
    cache = {
        "version": "phase3m_historical_evidence_v1",
        "prepared_at": cutoff.isoformat(),
        "lookahead_cutoff": cutoff.isoformat(),
        "entries": {
            "1": {"ticker": "READY", "model_name": "weather_v2", "historical_accuracy": 0.5}
        },
    }

    refreshed = preflight.refresh_unchanged_historical_cache(
        cache,
        {"READY": {}},
        latest_settlement_at=cutoff,
        refreshed_at=refreshed_at,
    )

    assert refreshed is not None
    assert refreshed["prepared_at"] == refreshed_at.isoformat()
    assert refreshed["entries"] == cache["entries"]
    assert refreshed["refresh_proof"]["prior_lookahead_cutoff"] == cutoff.isoformat()


def test_does_not_refresh_cache_when_new_settlement_exists() -> None:
    cutoff = datetime(2026, 8, 28, 5, tzinfo=UTC)
    cache = {
        "version": "phase3m_historical_evidence_v1",
        "lookahead_cutoff": cutoff.isoformat(),
        "entries": {"1": {"ticker": "READY", "model_name": "weather_v2"}},
    }

    assert (
        preflight.refresh_unchanged_historical_cache(
            cache,
            {"READY": {}},
            latest_settlement_at=cutoff + timedelta(seconds=1),
            refreshed_at=cutoff + timedelta(hours=7),
        )
        is None
    )


class _Savepoint:
    def __init__(self) -> None:
        self.rolled_back = False

    def rollback(self) -> None:
        self.rolled_back = True


class _Session:
    def __init__(self) -> None:
        self.savepoint = _Savepoint()
        self.flushes = 0

    def begin_nested(self):
        return self.savepoint

    def flush(self) -> None:
        self.flushes += 1


def test_shadow_phase3m_phase3n_evaluation_is_rolled_back(monkeypatch) -> None:
    session = _Session()
    raw = {
        "position_sizing_decision_id": 240,
        "advanced_risk_decision_id": 240,
        "position_sizing_decision": {"proposed_contracts": 1},
        "advanced_risk_decision": {"action": "BLOCK"},
    }

    def fake_size(active_session, decision, *, settings):
        assert active_session is session
        return SimpleNamespace(raw_decision_json=raw)

    monkeypatch.setattr(preflight, "ensure_paper_decision_sized", fake_size)

    result = preflight.evaluate_paper_decision_without_persisting(
        session, object(), settings=object()
    )

    assert session.flushes == 1
    assert session.savepoint.rolled_back is True
    assert result == raw
    assert result is not raw
