import json
from decimal import Decimal

from kalshi_predictor.crypto.providers import CryptoFetchResult, CryptoQuote
from kalshi_predictor.crypto.repository import get_crypto_prices, get_latest_crypto_features
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.single_writer_coordinator import (
    drain_staged_crypto_quotes,
    run_phase3bb_r43_single_writer_coordinator,
    stage_crypto_quote_fetches,
)
from kalshi_predictor.utils.time import utc_now


def test_stage_crypto_quote_fetches_writes_stage_files_without_db(tmp_path) -> None:
    staging_dir = tmp_path / "staging"

    result = stage_crypto_quote_fetches(
        symbols=["BTC", "ETH"],
        sources=["coinbase"],
        staging_dir=staging_dir,
        max_workers=2,
        fetch_crypto_quotes_fn=_fake_fetch_crypto_quotes,
    )

    assert result["status"] == "COMPLETE"
    assert len(result["staged_files"]) == 2
    staged = sorted(staging_dir.glob("crypto_quotes_*.json"))
    assert len(staged) == 2
    payload = json.loads(staged[0].read_text(encoding="utf-8"))
    assert payload["category"] == "crypto_quotes"
    assert payload["writes_database"] is False
    assert payload["quote_count"] == 1


def test_stage_crypto_quote_fetches_retries_transient_empty_result(tmp_path) -> None:
    attempts = 0

    def flaky_fetch(symbols, *, source="coinbase", timeout_seconds=10.0):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CryptoFetchResult(source=source, quotes=[], errors=["temporary failure"])
        return _fake_fetch_crypto_quotes(
            symbols,
            source=source,
            timeout_seconds=timeout_seconds,
        )

    result = stage_crypto_quote_fetches(
        symbols=["BTC"],
        sources=["coinbase"],
        staging_dir=tmp_path / "staging",
        max_attempts=3,
        retry_delay_seconds=0,
        fetch_crypto_quotes_fn=flaky_fetch,
    )

    assert result["status"] == "COMPLETE"
    assert result["jobs"][0]["attempts"] == 2
    assert attempts == 2


def test_coordinator_drains_staged_quotes_through_single_writer(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = tmp_path / "reports"

    artifacts = run_phase3bb_r43_single_writer_coordinator(
        session_factory=session_factory,
        output_dir=output_dir,
        symbols=["BTC"],
        crypto_sources=["coinbase"],
        drain_staged=True,
        fetch_crypto_quotes_fn=_fake_fetch_crypto_quotes,
        writer_monitor_fn=lambda: {"safe_to_start_write": True, "status": "CLEAR"},
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["single_writer_drain_enabled"] is True
    assert payload["drain_result"]["status"] == "COMPLETE"
    assert payload["drain_result"]["prices_inserted"] == 1
    assert payload["drain_result"]["features_inserted"] == 1
    with session_factory() as session:
        assert len(get_crypto_prices(session, "BTC")) == 1
        assert get_latest_crypto_features(session, "BTC") is not None


def test_coordinator_refuses_drain_when_writer_monitor_blocks(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = tmp_path / "reports"

    artifacts = run_phase3bb_r43_single_writer_coordinator(
        session_factory=session_factory,
        output_dir=output_dir,
        symbols=["BTC"],
        crypto_sources=["coinbase"],
        drain_staged=True,
        fetch_crypto_quotes_fn=_fake_fetch_crypto_quotes,
        writer_monitor_fn=lambda: {
            "safe_to_start_write": False,
            "status": "WRITER_ACTIVE",
            "current_writer_pid": 123,
        },
    )

    payload = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert payload["drain_result"]["status"] == "BLOCKED_ACTIVE_WRITER"
    assert payload["drain_result"]["prices_inserted"] == 0
    with session_factory() as session:
        assert get_crypto_prices(session, "BTC") == []


def test_drain_staged_crypto_quotes_imports_existing_stage_dir(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    staging_dir = tmp_path / "staging"
    stage_crypto_quote_fetches(
        symbols=["SOL"],
        sources=["coinbase"],
        staging_dir=staging_dir,
        fetch_crypto_quotes_fn=_fake_fetch_crypto_quotes,
    )

    with session_factory() as session:
        result = drain_staged_crypto_quotes(session, staging_dir=staging_dir)
        session.commit()

    assert result["prices_inserted"] == 1
    assert result["features_inserted"] == 1
    assert result["symbols"] == ["SOL"]


def test_drain_ignores_files_in_drained_archive(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    staging_dir = tmp_path / "staging"
    stage_crypto_quote_fetches(
        symbols=["BTC"],
        sources=["coinbase"],
        staging_dir=staging_dir,
        fetch_crypto_quotes_fn=_fake_fetch_crypto_quotes,
    )
    source = next(staging_dir.glob("crypto_quotes_*.json"))
    archive_dir = staging_dir / "drained"
    archive_dir.mkdir()
    (archive_dir / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with session_factory() as session:
        result = drain_staged_crypto_quotes(session, staging_dir=staging_dir)
        session.commit()

    assert result["files_seen"] == 1
    assert result["prices_inserted"] == 1


def _fake_fetch_crypto_quotes(
    symbols: list[str],
    *,
    source: str = "coinbase",
    timeout_seconds: float = 10.0,
) -> CryptoFetchResult:
    del timeout_seconds
    symbol = symbols[0]
    quote = CryptoQuote(
        symbol=symbol,
        source=source,
        observed_at=utc_now(),
        price_usd=Decimal("123.45"),
        volume_24h=None,
        market_cap=None,
        raw_json={"source": "test", "symbol": symbol},
    )
    return CryptoFetchResult(source=source, quotes=[quote], errors=[])


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{tmp_path / 'coordinator.db'}")
    return get_session_factory(engine)
