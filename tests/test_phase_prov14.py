import json
from datetime import UTC, datetime
from decimal import Decimal

from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.repository import insert_crypto_price
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.repositories import insert_forecast
from kalshi_predictor.data.schema import Market, MarketSnapshot, RuntimeProvenanceEvent
from kalshi_predictor.phase_prov14 import (
    build_prov14_certification_report,
    write_prov14_certification_report,
)


def test_prov14_certifies_only_exact_new_future_attribution(tmp_path, monkeypatch):
    _disable_memory_capture(monkeypatch)
    factory = _factory(tmp_path)
    now = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
    with factory() as session:
        session.add(Market(ticker="KXBTC-P14", raw_json="{}", first_seen_at=now, last_seen_at=now))
        insert_crypto_price(session, symbol="BTC", source="coinbase", observed_at=now,
                            price_usd="60000", raw_json={})
        build_crypto_features(session, symbols=["BTC"], source="coinbase")
        feature = session.execute(
            __import__("sqlalchemy").select(
                __import__("kalshi_predictor.data.schema", fromlist=["CryptoFeature"]).CryptoFeature
            )
        ).scalar_one()
        snapshot = MarketSnapshot(ticker="KXBTC-P14", captured_at=now, raw_market_json="{}")
        session.add(snapshot)
        session.flush()
        boundary = session.query(RuntimeProvenanceEvent).count()
        insert_forecast(session, {
            "ticker": "KXBTC-P14", "forecasted_at": now, "model_name": "crypto_v2",
            "yes_probability": Decimal("0.60"),
            "feature_json": {
                "crypto_feature_id": feature.id,
                "source_observation_ref": json.loads(feature.raw_json)["source_observation_ref"],
            },
        }, market_snapshot_id=snapshot.id, attribution_enabled=True)
        session.commit()
        report = build_prov14_certification_report(
            session, after_event_id=boundary, expected_models=("crypto_v2",), limit=10
        )

    assert report["summary"]["certification_passed"] is True
    assert report["summary"]["events_passed"] == 1
    assert report["database_writes_by_analyzer"] == 0


def test_prov14_blocks_missing_refs_and_missing_model_coverage(tmp_path, monkeypatch):
    _disable_memory_capture(monkeypatch)
    factory = _factory(tmp_path)
    now = datetime(2026, 7, 17, 18, 0, tzinfo=UTC)
    with factory() as session:
        session.add(Market(ticker="BROKEN-P14", raw_json="{}", first_seen_at=now, last_seen_at=now))
        insert_forecast(session, {
            "ticker": "BROKEN-P14", "forecasted_at": now, "model_name": "crypto_v2",
            "yes_probability": Decimal("0.50"), "feature_json": {},
        }, market_snapshot_id=None, attribution_enabled=True)
        session.commit()
        before = session.query(RuntimeProvenanceEvent).count()
        report = build_prov14_certification_report(
            session, after_event_id=0,
            expected_models=("crypto_v2", "weather_v2"), limit=10,
        )
        after = session.query(RuntimeProvenanceEvent).count()

    assert report["summary"]["certification_passed"] is False
    assert "weather_v2" in report["summary"]["missing_expected_models"]
    assert "SNAPSHOT_REFERENCE_MISSING" in report["rows"][0]["failures"]
    assert before == after


def test_prov14_report_write_is_atomic_and_database_read_only(tmp_path):
    factory = _factory(tmp_path)
    with factory() as session:
        path = write_prov14_certification_report(
            session, after_event_id=0, expected_models=(), output_dir=tmp_path / "report"
        )
        count = session.query(RuntimeProvenanceEvent).count()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["summary"]["certification_passed"] is False
    assert count == 0
    assert not path.with_suffix(".json.tmp").exists()


def _factory(tmp_path):
    return get_session_factory(init_db(f"sqlite:///{tmp_path / 'prov14.db'}"))


def _disable_memory_capture(monkeypatch):
    import kalshi_predictor.memory.capture as capture
    monkeypatch.setattr(capture, "capture_forecast_created", lambda *args, **kwargs: None)
