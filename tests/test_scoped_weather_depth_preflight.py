from __future__ import annotations

from types import SimpleNamespace

import scripts.scoped_weather_depth_preflight as preflight


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
