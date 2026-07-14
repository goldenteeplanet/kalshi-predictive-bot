from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    MarketSnapshot,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    SignalEvent,
    SignalForecast,
    SignalTrade,
)
from kalshi_predictor.signals.registry import (
    CRYPTO_NEXT_COMMANDS,
    ECONOMIC_NEXT_COMMANDS,
    NEWS_NEXT_COMMANDS,
    ExpectedSignal,
    ensure_builtin_signals,
    expected_signal_definitions,
)
from kalshi_predictor.signals.signal_types import (
    BREAKING_NEWS_SIGNAL,
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
)
from kalshi_predictor.signals.skip_log import (
    latest_skip_for_signal,
    log_signal_skip,
    signal_skip_row,
    skip_count_for_signal,
)

ACTIVE = "ACTIVE"
NEEDS_DATA = "NEEDS_DATA"
READY_NO_MARKETS = "READY_BUT_NO_MATCHING_MARKETS"
NOT_REGISTERED = "NOT_REGISTERED"


@dataclass(frozen=True)
class SignalStatusSummary:
    rows: list[dict[str, Any]]

    @property
    def active_signals(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["readiness_status"] == ACTIVE]

    @property
    def inactive_signals(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["readiness_status"] != ACTIVE]


def signal_status_rows(
    session: Session,
    *,
    definitions: Iterable[ExpectedSignal] | None = None,
    log_skips: bool = False,
) -> list[dict[str, Any]]:
    signals = {row.signal_name: row for row in ensure_builtin_signals(session)}
    rows = []
    for definition in definitions or expected_signal_definitions():
        signal = signals.get(definition.signal_name)
        counts = _counts(session, definition.signal_name)
        latest_generated = _latest_generated_time(session, definition.signal_name)
        readiness = _readiness_for_signal(session, definition, bool(signal), counts)
        next_command = readiness.pop("next_command_override", definition.next_command)
        latest_skip = signal_skip_row(latest_skip_for_signal(session, definition.signal_name))
        if log_skips and readiness["readiness_status"] != ACTIVE:
            latest_skip = signal_skip_row(
                log_signal_skip(
                    session,
                    signal_name=definition.signal_name,
                    ticker="*",
                    reason=readiness["skip_reason"],
                    required_data=definition.required_data,
                    available_data=readiness["available_data"],
                    raw_json={"signal_key": definition.key},
                )
            )
        rows.append(
            {
                "signal_key": definition.key,
                "signal_name": definition.signal_name,
                "registered": bool(signal),
                "registered_label": "yes" if signal else "no",
                "category": signal.category if signal else "Unknown",
                "forecast_count": counts["forecast_count"],
                "trade_count": counts["trade_count"],
                "event_count": counts["event_count"],
                "latest_generated_time": latest_generated.isoformat()
                if latest_generated
                else None,
                "latest_signal": latest_generated.isoformat() if latest_generated else "none",
                "required_data": definition.required_data,
                "next_command": next_command,
                "next_action": next_command,
                "skip_count": skip_count_for_signal(session, definition.signal_name),
                "skip_reason": (latest_skip or {}).get("reason") or readiness["skip_reason"],
                "latest_skip_time": (latest_skip or {}).get("skipped_at"),
                **readiness,
            }
        )
    return sorted(rows, key=_status_sort_key)


def signal_status_summary(session: Session, *, log_skips: bool = False) -> SignalStatusSummary:
    return SignalStatusSummary(rows=signal_status_rows(session, log_skips=log_skips))


def signal_health_context(session: Session) -> dict[str, Any]:
    rows = signal_status_rows(session)
    active = [row for row in rows if row["readiness_status"] == ACTIVE]
    inactive = [row for row in rows if row["readiness_status"] != ACTIVE]
    return {
        "rows": rows,
        "active_signals": active,
        "inactive_signals": inactive,
        "missing_data": [row for row in rows if row["missing_data"] != "none"],
        "summary": {
            "active": len(active),
            "inactive": len(inactive),
            "skip_count": sum(int(row["skip_count"]) for row in rows),
        },
    }


def _readiness_for_signal(
    session: Session,
    definition: ExpectedSignal,
    registered: bool,
    counts: dict[str, int],
) -> dict[str, Any]:
    if not registered:
        return _readiness(
            NOT_REGISTERED,
            "Signal is not registered.",
            "signal registry row",
            "signal registry",
            {},
        )
    if counts["forecast_count"] > 0 or counts["trade_count"] > 0 or counts["event_count"] > 0:
        return _readiness(ACTIVE, "Signal is producing output.", "none", "none", {})
    if definition.signal_name == CRYPTO_SIGNAL:
        return _crypto_readiness(session)
    if definition.signal_name == ECONOMIC_SIGNAL:
        return _economic_readiness(session)
    if definition.signal_name in {BREAKING_NEWS_SIGNAL, CRYPTO_NEWS_SIGNAL, ECONOMIC_NEWS_SIGNAL}:
        return _news_readiness(session)
    if definition.signal_name == MARKET_DIVERGENCE_SIGNAL:
        if _count(session, Forecast) == 0:
            return _needs(
                "no forecasts",
                "forecasts",
                {"snapshots": _count(session, MarketSnapshot)},
            )
        if _count(session, MarketSnapshot) == 0:
            return _needs("no market snapshots", "market snapshots", {"forecasts": True})
        return _ready_no_markets("no model/market divergence crossed threshold")
    if definition.signal_name in {FRESH_DATA_SIGNAL, SPREAD_COMPRESSION_SIGNAL}:
        if _count(session, MarketSnapshot) == 0:
            return _needs("no market snapshots", "market snapshots", {})
        return _ready_no_markets("no matching fresh/tight-spread snapshots yet")
    if definition.signal_name == ENSEMBLE_AGREEMENT_SIGNAL:
        if _forecast_count_like(session, "ensemble%") == 0:
            return _needs("no ensemble forecasts", "ensemble forecasts", {})
        return _ready_no_markets("ensemble forecasts exist but no agreement signal was attributed")
    if definition.signal_name in {META_SELECTION_SIGNAL, MODEL_TRUST_SIGNAL}:
        if _forecast_count_like(session, "meta%") == 0:
            return _needs("no meta model forecasts", "meta model forecasts", {})
        return _ready_no_markets("meta forecasts exist but this signal was not attributed")
    return _ready_no_markets("ready but no matching markets")


def _crypto_readiness(session: Session) -> dict[str, Any]:
    link_count = _count(session, CryptoMarketLink)
    feature_count = _count(session, CryptoFeature)
    snapshot_count = _count(session, MarketSnapshot)
    available = {
        "crypto_links": link_count,
        "crypto_features": feature_count,
        "market_snapshots": snapshot_count,
    }
    if link_count == 0:
        return _needs("no crypto links", "crypto market links", available, CRYPTO_NEXT_COMMANDS)
    if feature_count == 0:
        return _needs("no crypto features", "crypto_features", available, CRYPTO_NEXT_COMMANDS)
    if snapshot_count == 0:
        return _needs(
            "no crypto market snapshots",
            "latest market snapshot",
            available,
            CRYPTO_NEXT_COMMANDS,
        )
    return _ready_no_markets("ready but no crypto-linked forecast attribution yet", available)


def _economic_readiness(session: Session) -> dict[str, Any]:
    link_count = _count(session, EconomicMarketLink)
    feature_count = _count(session, EconomicFeature)
    snapshot_count = _count(session, MarketSnapshot)
    available = {
        "economic_links": link_count,
        "economic_features": feature_count,
        "market_snapshots": snapshot_count,
    }
    if link_count == 0:
        return _needs(
            "no economic links",
            "economic market links",
            available,
            ECONOMIC_NEXT_COMMANDS,
        )
    if feature_count == 0:
        return _needs(
            "no economic features",
            "economic_features",
            available,
            ECONOMIC_NEXT_COMMANDS,
        )
    if snapshot_count == 0:
        return _needs(
            "no economic market snapshots",
            "latest market snapshot",
            available,
            ECONOMIC_NEXT_COMMANDS,
        )
    return _ready_no_markets("ready but no economic-linked forecast attribution yet", available)


def _news_readiness(session: Session) -> dict[str, Any]:
    item_count = _count(session, NewsItem)
    link_count = _count(session, NewsMarketLink)
    feature_count = _count(session, NewsFeature)
    available = {
        "news_items": item_count,
        "news_market_links": link_count,
        "news_features": feature_count,
    }
    if item_count == 0:
        return _needs("no news items", "news_items", available, NEWS_NEXT_COMMANDS)
    if link_count == 0:
        return _needs("no news market links", "news_market_links", available, NEWS_NEXT_COMMANDS)
    if feature_count == 0:
        return _needs("no news features", "news_features", available, NEWS_NEXT_COMMANDS)
    return _ready_no_markets("ready but no matching news signal yet", available)


def _needs(
    reason: str,
    missing_data: str,
    available_data: dict[str, Any],
    next_command: str | None = None,
) -> dict[str, Any]:
    row = _readiness(NEEDS_DATA, reason, missing_data, reason, available_data)
    if next_command is not None:
        row["next_command_override"] = next_command
    return row


def _ready_no_markets(
    reason: str,
    available_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _readiness(
        READY_NO_MARKETS,
        reason,
        "none",
        reason,
        available_data or {},
    )


def _readiness(
    status: str,
    label_reason: str,
    missing_data: str,
    skip_reason: str,
    available_data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ready": status == ACTIVE,
        "readiness_status": status,
        "status_label": _status_label(status),
        "missing_data": missing_data,
        "skip_reason": skip_reason,
        "available_data": available_data,
        "readiness_reason": label_reason,
    }


def _status_label(status: str) -> str:
    return {
        ACTIVE: "Active",
        NEEDS_DATA: "Needs data",
        READY_NO_MARKETS: "Ready but no matching markets",
        NOT_REGISTERED: "Not registered",
    }.get(status, status)


def _counts(session: Session, signal_name: str) -> dict[str, int]:
    return {
        "forecast_count": _count_where(
            session,
            SignalForecast,
            SignalForecast.signal_name,
            signal_name,
        ),
        "trade_count": _count_where(session, SignalTrade, SignalTrade.signal_name, signal_name),
        "event_count": _count_where(session, SignalEvent, SignalEvent.signal_name, signal_name),
    }


def _latest_generated_time(session: Session, signal_name: str):
    values = [
        session.scalar(
            select(SignalEvent.created_at)
            .where(SignalEvent.signal_name == signal_name)
            .order_by(desc(SignalEvent.created_at), desc(SignalEvent.id))
            .limit(1)
        ),
        session.scalar(
            select(SignalForecast.created_at)
            .where(SignalForecast.signal_name == signal_name)
            .order_by(desc(SignalForecast.created_at), desc(SignalForecast.id))
            .limit(1)
        ),
        session.scalar(
            select(SignalTrade.created_at)
            .where(SignalTrade.signal_name == signal_name)
            .order_by(desc(SignalTrade.created_at), desc(SignalTrade.id))
            .limit(1)
        ),
    ]
    timestamps = [value for value in values if value is not None]
    return max(timestamps) if timestamps else None


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _count_where(session: Session, model: type, column: Any, value: str) -> int:
    return int(
        session.scalar(select(func.count()).select_from(model).where(column == value))
        or 0
    )


def _forecast_count_like(session: Session, pattern: str) -> int:
    return int(
        session.scalar(
            select(func.count()).select_from(Forecast).where(Forecast.model_name.like(pattern))
        )
        or 0
    )


def _status_sort_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    status_order = {
        ACTIVE: 0,
        READY_NO_MARKETS: 1,
        NEEDS_DATA: 2,
        NOT_REGISTERED: 3,
    }
    activity = int(row["forecast_count"]) + int(row["trade_count"]) + int(row["event_count"])
    return (
        status_order.get(row["readiness_status"], 9),
        -activity,
        -int(row["skip_count"]),
        row["signal_name"],
    )
