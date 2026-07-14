from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    EconomicFeature,
    EconomicMarketLink,
    Forecast,
    MarketSnapshot,
    MetaModelFeature,
    MicrostructureFeature,
    NewsFeature,
    NewsItem,
    NewsMarketLink,
    SportsFeature,
    SportsMarketLink,
    WeatherFeature,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.registry import MODEL_NAMES, get_forecaster
from kalshi_predictor.forecasting.skip_log import (
    forecast_skip_row,
    latest_skip_for_model,
    skip_count_for_model,
)
from kalshi_predictor.utils.time import utc_now

STATUS_ACTIVE = "ACTIVE"
STATUS_NEEDS_DATA = "NEEDS_DATA"
STATUS_READY_NO_MATCHING_MARKETS = "READY_BUT_NO_MATCHING_MARKETS"
STATUS_READY_NO_FORECASTS = "READY_NO_FORECASTS"
STATUS_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
STATUS_ERROR = "ERROR"

EXPECTED_MODEL_NAMES = (
    "market_implied_v1",
    "ensemble_v1",
    "ensemble_v2",
    "crypto_v2",
    "weather_v2",
    "economic_v1",
    "news_v1",
    "sports_v1",
    "microstructure_v1",
    "meta_v1",
)
CORE_MODEL_NAMES = EXPECTED_MODEL_NAMES

MODEL_ALIASES = {
    "meta_v1": ("meta_model_v1", "meta_ensemble_v1"),
}

REQUIRED_DATA = {
    "market_implied_v1": "latest market snapshot with midpoint",
    "ensemble_v1": "at least one component forecast",
    "ensemble_v2": "component forecasts or market-implied fallback",
    "crypto_v2": "crypto market link, crypto features, market snapshot",
    "weather_v2": "weather market link, weather features, market snapshot",
    "economic_v1": "economic market link, economic features, market snapshot",
    "news_v1": "news items, news market links, news features, market snapshot",
    "sports_v1": "sports market link, sports features, market snapshot",
    "microstructure_v1": "microstructure features, market snapshot",
    "meta_v1": "meta model features, market snapshot",
}

NEXT_ACTIONS = {
    "market_implied_v1": "Run collect-once or snapshot to store market snapshots.",
    "ensemble_v1": "Run forecast --model all after component models are ready.",
    "ensemble_v2": "Run forecast --model all after component models are ready.",
    "crypto_v2": "Run ingest-crypto, build-crypto-features, link-crypto-markets.",
    "weather_v2": "Run ingest-weather, build-weather-features, link-weather-markets.",
    "economic_v1": "Load economic sample data or connect economic calendar ingestion.",
    "news_v1": "Run ingest-news, build-news-features, link-news-markets.",
    "sports_v1": "Run ingest-sports, build-sports-features, link-sports-markets.",
    "microstructure_v1": "Run build-microstructure-features, then forecast microstructure_v1.",
    "meta_v1": "Run forecast --model meta_model_v1 and forecast --model meta_ensemble_v1.",
}

NEXT_COMMANDS = {
    "market_implied_v1": [
        "kalshi-bot collect-once --status open --limit 100 --max-pages 1",
        "kalshi-bot forecast --model market_implied_v1",
    ],
    "ensemble_v1": [
        "kalshi-bot forecast --model all",
    ],
    "ensemble_v2": [
        "kalshi-bot forecast --model all",
    ],
    "crypto_v2": [
        f"kalshi-bot ingest-crypto --symbols {DEFAULT_CRYPTO_SYMBOLS} --source coinbase",
        f"kalshi-bot build-crypto-features --symbols {DEFAULT_CRYPTO_SYMBOLS}",
        "kalshi-bot link-crypto-markets",
        "kalshi-bot forecast --model crypto_v2",
    ],
    "weather_v2": [
        "kalshi-bot ingest-weather --location-key kansas_city",
        "kalshi-bot build-weather-features --location-key kansas_city",
        "kalshi-bot link-weather-markets",
        "kalshi-bot forecast --model weather_v2",
    ],
    "economic_v1": [
        "kalshi-bot ingest-economic --input-file data/economic_sample.json",
        "kalshi-bot build-economic-features",
        "kalshi-bot link-economic-markets",
        "kalshi-bot forecast --model economic_v1",
    ],
    "news_v1": [
        "kalshi-bot ingest-news --input-file data/news_sample.json",
        "kalshi-bot build-news-features",
        "kalshi-bot forecast --model news_v1",
    ],
    "sports_v1": [
        "kalshi-bot ingest-sports --league nfl --input-file data/sports_sample.json",
        "kalshi-bot build-sports-features",
        "kalshi-bot forecast --model sports_v1",
    ],
    "microstructure_v1": [
        "kalshi-bot build-microstructure-features",
        "kalshi-bot forecast --model microstructure_v1",
    ],
    "meta_v1": [
        "kalshi-bot forecast --model meta_model_v1",
        "kalshi-bot forecast --model meta_ensemble_v1",
    ],
}


@dataclass(frozen=True)
class ModelStatusSummary:
    rows: list[dict[str, Any]]

    @property
    def inactive_models(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["status"] != STATUS_ACTIVE]

    @property
    def active_models(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if row["status"] == STATUS_ACTIVE]


def model_status_rows(
    session: Session,
    *,
    model_names: Iterable[str] = CORE_MODEL_NAMES,
) -> list[dict[str, Any]]:
    rows = []
    data_counts = _readiness_counts(session)
    for model_name in model_names:
        registered = _is_registered(model_name)
        count = _forecast_count(session, model_name)
        latest = _latest_forecast_time(session, model_name)
        skip = forecast_skip_row(latest_skip_for_model(session, model_name))
        skip_count = skip_count_for_model(session, model_name)
        requirements = _requirements_for_model(model_name, data_counts)
        missing_data = requirements["missing_data"]
        status = _readiness_status(
            registered=registered,
            forecast_count=count,
            missing_data=missing_data,
            available_data=requirements["available_data"],
        )
        latest_label = latest.isoformat() if latest else None
        next_commands = NEXT_COMMANDS.get(model_name, ["kalshi-bot forecast --model all"])
        rows.append(
            {
                "model_name": model_name,
                "stored_model_names": list(_stored_model_names(model_name)),
                "registered": registered,
                "registered_label": "yes" if registered else "no",
                "forecast_count": count,
                "latest_forecast_time": latest_label,
                "latest_forecast_at": latest_label,
                "required_data": REQUIRED_DATA.get(model_name, "model-specific data"),
                "available_data": requirements["available_data"],
                "available_data_label": _format_counts(requirements["available_data"]),
                "missing_data": missing_data,
                "missing_data_label": ", ".join(missing_data) if missing_data else "none",
                "ready": status == STATUS_ACTIVE,
                "status": status,
                "status_label": _status_label(status),
                "readiness_status": status,
                "readiness_status_label": _status_label(status),
                "skip_reason": (skip or {}).get("reason") or "No skip logged yet.",
                "latest_skip_time": (skip or {}).get("skipped_at"),
                "skip_count": skip_count,
                "next_action": NEXT_ACTIONS.get(model_name, "Run forecast --model all."),
                "next_commands": next_commands,
                "next_commands_label": " | ".join(next_commands),
                "last_error": (skip or {}).get("reason"),
            }
        )
    return rows


def model_status_summary(session: Session) -> ModelStatusSummary:
    return ModelStatusSummary(rows=model_status_rows(session))


def generate_model_readiness_report(
    session: Session,
    *,
    output_path: Path = Path("reports/model_readiness.md"),
) -> Path:
    summary = model_status_summary(session)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_model_readiness_report(summary), encoding="utf-8")
    return output_path


def render_model_readiness_report(summary: ModelStatusSummary) -> str:
    status_counts: dict[str, int] = {}
    for row in summary.rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1

    lines = [
        "# Model Readiness Report",
        "",
        f"Generated at: {utc_now().isoformat()}",
        "",
        "## Summary",
        "",
    ]
    for status in (
        STATUS_ACTIVE,
        STATUS_READY_NO_FORECASTS,
        STATUS_READY_NO_MATCHING_MARKETS,
        STATUS_NEEDS_DATA,
        STATUS_NOT_IMPLEMENTED,
        STATUS_ERROR,
    ):
        lines.append(f"- {status}: {status_counts.get(status, 0)}")

    lines.extend(
        [
            "",
            "## Model Readiness",
            "",
            "| Model | Status | Forecasts | Latest Forecast | Missing Data | Next Commands |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in summary.rows:
        lines.append(
            " | ".join(
                (
                    f"| {_md(row['model_name'])}",
                    _md(row["status"]),
                    str(row["forecast_count"]),
                    _md(row["latest_forecast_time"] or "none"),
                    _md(row["missing_data_label"]),
                    _md("<br>".join(row["next_commands"])),
                )
            )
            + " |"
        )

    lines.extend(["", "## Recommended Next Actions", ""])
    for row in summary.inactive_models:
        lines.append(f"- {row['model_name']}: {row['next_action']}")
        for command in row["next_commands"]:
            lines.append(f"  - `{command}`")
    if not summary.inactive_models:
        lines.append("- All expected models are active.")
    lines.append("")
    return "\n".join(lines)


def _is_registered(model_name: str) -> bool:
    return all(_can_create(stored_name) for stored_name in _stored_model_names(model_name))


def _can_create(model_name: str) -> bool:
    if model_name not in MODEL_NAMES:
        return False
    try:
        get_forecaster(model_name)
    except Exception:
        return False
    return True


def _stored_model_names(model_name: str) -> tuple[str, ...]:
    return MODEL_ALIASES.get(model_name, (model_name,))


def _forecast_count(session: Session, model_name: str) -> int:
    model_names = _stored_model_names(model_name)
    return int(
        session.scalar(
            select(func.count()).select_from(Forecast).where(Forecast.model_name.in_(model_names))
        )
        or 0
    )


def _latest_forecast_time(session: Session, model_name: str):
    model_names = _stored_model_names(model_name)
    return session.scalar(
        select(Forecast.forecasted_at)
        .where(Forecast.model_name.in_(model_names))
        .order_by(desc(Forecast.forecasted_at), desc(Forecast.id))
        .limit(1)
    )


def _readiness_counts(session: Session) -> dict[str, int]:
    component_models = tuple(
        name
        for name in MODEL_NAMES
        if name
        not in {
            "ensemble_v1",
            "ensemble_v2",
            "meta_model_v1",
            "meta_ensemble_v1",
        }
    )
    return {
        "market_snapshots": _table_count(session, MarketSnapshot),
        "component_forecasts": int(
            session.scalar(
                select(func.count())
                .select_from(Forecast)
                .where(Forecast.model_name.in_(component_models))
            )
            or 0
        ),
        "crypto_market_links": _table_count(session, CryptoMarketLink),
        "crypto_features": _table_count(session, CryptoFeature),
        "weather_market_links": _table_count(session, WeatherMarketLink),
        "weather_features": _table_count(session, WeatherFeature),
        "economic_market_links": _table_count(session, EconomicMarketLink),
        "economic_features": _table_count(session, EconomicFeature),
        "news_items": _table_count(session, NewsItem),
        "news_market_links": _table_count(session, NewsMarketLink),
        "news_features": _table_count(session, NewsFeature),
        "sports_market_links": _table_count(session, SportsMarketLink),
        "sports_features": _table_count(session, SportsFeature),
        "microstructure_features": _table_count(session, MicrostructureFeature),
        "meta_model_features": _table_count(session, MetaModelFeature),
    }


def _requirements_for_model(model_name: str, data_counts: dict[str, int]) -> dict[str, Any]:
    if model_name in {"ensemble_v1", "ensemble_v2"}:
        available = {
            "component_forecasts": data_counts["component_forecasts"],
            "market_snapshots": data_counts["market_snapshots"],
        }
        missing = (
            []
            if available["component_forecasts"] > 0 or available["market_snapshots"] > 0
            else ["component forecasts or market-implied fallback"]
        )
        return {"available_data": available, "missing_data": missing}

    keys_by_model = {
        "market_implied_v1": (("market snapshot", "market_snapshots"),),
        "crypto_v2": (
            ("crypto market link", "crypto_market_links"),
            ("crypto features", "crypto_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "weather_v2": (
            ("weather market link", "weather_market_links"),
            ("weather features", "weather_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "economic_v1": (
            ("economic market link", "economic_market_links"),
            ("economic features", "economic_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "news_v1": (
            ("news items", "news_items"),
            ("news market links", "news_market_links"),
            ("news features", "news_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "sports_v1": (
            ("sports market link", "sports_market_links"),
            ("sports features", "sports_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "microstructure_v1": (
            ("microstructure features", "microstructure_features"),
            ("market snapshot", "market_snapshots"),
        ),
        "meta_v1": (
            ("meta model features", "meta_model_features"),
            ("market snapshot", "market_snapshots"),
        ),
    }
    requirement_keys = keys_by_model.get(model_name, ())
    available = {key: data_counts.get(key, 0) for _, key in requirement_keys}
    missing = [label for label, key in requirement_keys if data_counts.get(key, 0) <= 0]
    return {"available_data": available, "missing_data": missing}


def _readiness_status(
    *,
    registered: bool,
    forecast_count: int,
    missing_data: list[str],
    available_data: dict[str, int],
) -> str:
    if not registered:
        return STATUS_NOT_IMPLEMENTED
    if forecast_count > 0:
        return STATUS_ACTIVE
    if missing_data:
        if _has_data_but_no_matching_markets(missing_data, available_data):
            return STATUS_READY_NO_MATCHING_MARKETS
        return STATUS_NEEDS_DATA
    return STATUS_READY_NO_FORECASTS


def _has_data_but_no_matching_markets(
    missing_data: list[str], available_data: dict[str, int]
) -> bool:
    if not missing_data:
        return False
    if not all("market link" in label for label in missing_data):
        return False
    source_counts = [
        count
        for key, count in available_data.items()
        if "market_link" not in key and "market_links" not in key
    ]
    return bool(source_counts) and all(count > 0 for count in source_counts)


def _table_count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in counts.items())


def _status_label(status: str) -> str:
    return {
        STATUS_ACTIVE: "Active",
        STATUS_READY_NO_MATCHING_MARKETS: "Ready, no matching markets",
        STATUS_READY_NO_FORECASTS: "Ready, no forecasts yet",
        STATUS_NEEDS_DATA: "Needs data",
        STATUS_NOT_IMPLEMENTED: "Not implemented",
        STATUS_ERROR: "Error",
    }.get(status, status)


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")
