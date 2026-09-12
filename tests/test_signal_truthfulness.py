from datetime import timedelta

from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.schema import SignalEvent
from kalshi_predictor.signals.registry import ensure_builtin_signals
from kalshi_predictor.signals.repository import _signal_card, signal_marketplace
from kalshi_predictor.signals.status import signal_status_rows
from kalshi_predictor.utils.time import utc_now


def test_all_builtin_signals_have_explicit_unknown_or_evidenced_readiness(tmp_path):
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'truth.db'}"))
    with factory() as session:
        signals = ensure_builtin_signals(session)
        rows = signal_status_rows(session)
        assert {s.signal_name for s in signals} == {r["signal_name"] for r in rows}
        assert all(r["readiness_status"] != "ACTIVE" for r in rows)
        card = _signal_card(signals[0], None)
        assert card["status"] == "Readiness unknown"
        assert card["missing_data"] == "readiness evidence"
        assert card["model_readiness"] == "UNVERIFIED"


def test_old_output_cannot_appear_active_and_future_clock_is_unknown(tmp_path):
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'stale.db'}"))
    with factory() as session:
        signal = ensure_builtin_signals(session)[0]
        event = SignalEvent(
            created_at=utc_now() - timedelta(days=30),
            ticker="TEST",
            signal_name=signal.signal_name,
            signal_strength="1",
            confidence="60",
            raw_json="{}",
        )
        session.add(event)
        session.flush()
        row = next(r for r in signal_status_rows(session) if r["signal_name"] == signal.signal_name)
        assert row["readiness_status"] == "STALE"
        event.created_at = utc_now() + timedelta(hours=1)
        session.flush()
        row = next(r for r in signal_status_rows(session) if r["signal_name"] == signal.signal_name)
        assert row["readiness_status"] == "UNKNOWN"


def test_marketplace_separates_mission_unknown_from_historical_zero(tmp_path):
    factory = get_session_factory(init_db(f"sqlite:///{tmp_path / 'counts.db'}"))
    with factory() as session:
        summary = signal_marketplace(session)["summary"]
        assert summary["unique_historical_orders"] == 0
        assert summary["mission_trades"] is None
