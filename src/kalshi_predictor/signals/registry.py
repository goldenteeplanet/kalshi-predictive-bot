from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.data.repositories import encode_json
from kalshi_predictor.data.schema import Signal
from kalshi_predictor.signals.signal_types import (
    BREAKING_NEWS_SIGNAL,
    BUILTIN_SIGNALS,
    CRYPTO_NEWS_SIGNAL,
    CRYPTO_SIGNAL,
    ECONOMIC_NEWS_SIGNAL,
    ECONOMIC_SIGNAL,
    ENSEMBLE_AGREEMENT_SIGNAL,
    FRESH_DATA_SIGNAL,
    MARKET_DIVERGENCE_SIGNAL,
    META_SELECTION_SIGNAL,
    MODEL_TRUST_SIGNAL,
    SPREAD_COMPRESSION_SIGNAL,
    SignalDefinition,
)
from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class ExpectedSignal:
    key: str
    signal_name: str
    required_data: str
    next_command: str


CRYPTO_NEXT_COMMANDS = (
    f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase; "
    f"kalshi-bot build-crypto-features --symbols {DEFAULT_CRYPTO_SYMBOLS}; "
    "kalshi-bot link-crypto-markets"
)

ECONOMIC_NEXT_COMMANDS = (
    "kalshi-bot ingest-economic --input-file data/economic_sample.json; "
    "kalshi-bot build-economic-features; "
    "kalshi-bot link-economic-markets"
)

NEWS_NEXT_COMMANDS = (
    "News ingestion not connected yet. "
    "kalshi-bot ingest-news --input-file data/news_sample.json; "
    "kalshi-bot build-news-features; "
    "kalshi-bot forecast-signals --signal breaking_news"
)

EXPECTED_SIGNALS = (
    ExpectedSignal(
        "market_divergence",
        MARKET_DIVERGENCE_SIGNAL,
        "forecasts and latest market snapshot with tradable price",
        "kalshi-bot collect-once --status open --limit 100 --max-pages 1; "
        "kalshi-bot forecast --model all",
    ),
    ExpectedSignal(
        "fresh_data",
        FRESH_DATA_SIGNAL,
        "latest market snapshot",
        "kalshi-bot collect-once --status open --limit 100 --max-pages 1",
    ),
    ExpectedSignal(
        "spread_compression",
        SPREAD_COMPRESSION_SIGNAL,
        "latest market snapshot with bid/ask spread",
        "kalshi-bot collect-once --status open --limit 100 --max-pages 1",
    ),
    ExpectedSignal(
        "ensemble_agreement",
        ENSEMBLE_AGREEMENT_SIGNAL,
        "ensemble forecasts with component probabilities",
        "kalshi-bot forecast --model all",
    ),
    ExpectedSignal(
        "model_trust",
        MODEL_TRUST_SIGNAL,
        "meta model decisions with trust scores",
        "kalshi-bot build-meta-features; kalshi-bot forecast --model meta_model_v1",
    ),
    ExpectedSignal(
        "meta_selection",
        META_SELECTION_SIGNAL,
        "meta model selections",
        "kalshi-bot build-meta-features; kalshi-bot forecast --model meta_model_v1",
    ),
    ExpectedSignal(
        "crypto",
        CRYPTO_SIGNAL,
        "crypto market links, crypto_features, latest market snapshot",
        CRYPTO_NEXT_COMMANDS,
    ),
    ExpectedSignal(
        "crypto_news",
        CRYPTO_NEWS_SIGNAL,
        "crypto news items, news market links, news features",
        NEWS_NEXT_COMMANDS,
    ),
    ExpectedSignal(
        "economic",
        ECONOMIC_SIGNAL,
        "economic market links, economic_features, latest market snapshot",
        ECONOMIC_NEXT_COMMANDS,
    ),
    ExpectedSignal(
        "economic_news",
        ECONOMIC_NEWS_SIGNAL,
        "economic news items, news market links, news features",
        NEWS_NEXT_COMMANDS,
    ),
    ExpectedSignal(
        "breaking_news",
        BREAKING_NEWS_SIGNAL,
        "news items, news market links, news features",
        NEWS_NEXT_COMMANDS,
    ),
)


def ensure_builtin_signals(session: Session) -> list[Signal]:
    return ensure_signals(session, BUILTIN_SIGNALS)


def ensure_signals(
    session: Session,
    definitions: Iterable[SignalDefinition],
) -> list[Signal]:
    rows: list[Signal] = []
    for definition in definitions:
        rows.append(upsert_signal(session, definition))
    session.flush()
    return rows


def upsert_signal(session: Session, definition: SignalDefinition) -> Signal:
    signal = _pending_signal(session, definition.signal_name) or session.scalar(
        select(Signal).where(Signal.signal_name == definition.signal_name)
    )
    if signal is None:
        signal = Signal(
            created_at=utc_now(),
            signal_name=definition.signal_name,
            category=definition.category,
            description=definition.description,
            status=definition.status,
            metadata_json=encode_json(definition.metadata),
        )
        session.add(signal)
    else:
        signal.category = definition.category
        signal.description = definition.description
        signal.status = definition.status
        signal.metadata_json = encode_json(definition.metadata)
    return signal


def builtin_signal_names() -> list[str]:
    return [definition.signal_name for definition in BUILTIN_SIGNALS]


def expected_signal_definitions() -> tuple[ExpectedSignal, ...]:
    return EXPECTED_SIGNALS


def expected_signal_names() -> list[str]:
    return [definition.signal_name for definition in EXPECTED_SIGNALS]


def expected_signal_by_key(key: str) -> ExpectedSignal | None:
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    for definition in EXPECTED_SIGNALS:
        if definition.key == normalized:
            return definition
    return None


def _pending_signal(session: Session, signal_name: str) -> Signal | None:
    for item in session.new:
        if isinstance(item, Signal) and item.signal_name == signal_name:
            return item
    return None
