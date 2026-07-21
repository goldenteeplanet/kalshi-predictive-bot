from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session, aliased

from kalshi_predictor.active_universe import is_inactive_market_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.repository import parse_symbols
from kalshi_predictor.data.locks import db_writer_monitor
from kalshi_predictor.data.schema import (
    Market,
    MarketRanking,
    MarketSnapshot,
    PaperOrder,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.registry import (
    latest_snapshots_for_model,
    run_forecast_models,
)
from kalshi_predictor.ingest.websocket_orderbooks import (
    drain_staged_websocket_orderbooks,
)
from kalshi_predictor.opportunities.scanner import scan_opportunities
from kalshi_predictor.phase3ba_r3 import build_phase3ba_r3_weather_paper_gate
from kalshi_predictor.phase3bc_r5 import (
    write_phase3bc_r5_crypto_freshness_watch_report,
)
from kalshi_predictor.single_writer_coordinator import (
    drain_staged_crypto_quotes,
    stage_crypto_quote_fetches,
)
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.linker import WEATHER_TICKER_PREFIXES, link_weather_markets

PHASE_GH2_VERSION = "GH-2.0"
CRYPTO_TICKER_PREFIXES = ("KXBTC", "KXETH", "KXSOLE", "KXXRP", "KXDOGE")
ACTIONABLE_MODELS = ("crypto_v2", "weather_v2")
PAPER_ONLY_SAFETY = "PAPER_ONLY_NO_ORDER_CREATION_OR_EXCHANGE_WRITES"


@dataclass(frozen=True)
class GH2Artifacts:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    history_path: Path
    candidate_manifest_path: Path


def stage_gh2_crypto_quotes(
    *,
    staging_dir: Path,
    symbols: str = DEFAULT_CRYPTO_SYMBOLS,
    sources: str = "coinbase",
    max_workers: int = 4,
) -> dict[str, Any]:
    """Fetch external quotes in parallel into files without touching SQLite."""

    result = stage_crypto_quote_fetches(
        symbols=parse_symbols(symbols),
        sources=_parse_csv(sources),
        staging_dir=staging_dir,
        max_workers=max_workers,
    )
    payload = {
        "phase": "GH-2-STAGE",
        "generated_at": utc_now().isoformat(),
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "database_writes": 0,
        "orders_created": 0,
        **result,
    }
    _write_json(staging_dir / "stage_status.json", payload)
    return payload


def select_actionable_ranked_markets(
    session: Session,
    *,
    limit: int = 40,
    max_per_series: int = 6,
    max_ranking_age_hours: int = 24,
    freshness_minutes: int = 15,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Select active ranked books, favoring fresh executable positive-edge rows."""

    resolved_now = _aware(now or utc_now())
    cutoff = resolved_now - timedelta(hours=max(max_ranking_age_hours, 1))
    statement = (
        select(MarketRanking)
        .where(
            MarketRanking.forecast_model.in_(ACTIONABLE_MODELS),
            MarketRanking.ranked_at >= cutoff,
        )
        .order_by(
            desc(MarketRanking.ranked_at),
            desc(MarketRanking.opportunity_score),
            desc(MarketRanking.id),
        )
        .limit(max(limit * 100, 2000))
    )
    latest_rankings: list[MarketRanking] = []
    seen: set[str] = set()
    for ranking in session.scalars(statement):
        if ranking.ticker in seen:
            continue
        seen.add(ranking.ticker)
        latest_rankings.append(ranking)

    tickers = [ranking.ticker for ranking in latest_rankings]
    markets = (
        {
            market.ticker: market
            for market in session.scalars(select(Market).where(Market.ticker.in_(tickers)))
        }
        if tickers
        else {}
    )
    snapshots = _latest_snapshots(session, tickers)
    candidates: list[dict[str, Any]] = []
    for ranking in latest_rankings:
        market = markets.get(ranking.ticker)
        if market is None or is_inactive_market_status(market.status):
            continue
        if market.close_time is not None and _aware(market.close_time) <= resolved_now:
            continue
        snapshot = snapshots.get(ranking.ticker)
        snapshot_age_minutes = (
            max(0.0, (resolved_now - _aware(snapshot.captured_at)).total_seconds() / 60)
            if snapshot is not None
            else None
        )
        edge = _decimal(ranking.estimated_edge)
        executable = bool(ranking.best_side and ranking.best_price)
        fresh = snapshot_age_minutes is not None and snapshot_age_minutes <= freshness_minutes
        candidates.append(
            {
                "ticker": ranking.ticker,
                "series_ticker": market.series_ticker or ranking.series_ticker,
                "model": ranking.forecast_model,
                "ranked_at": _aware(ranking.ranked_at).isoformat(),
                "snapshot_at": (
                    _aware(snapshot.captured_at).isoformat() if snapshot is not None else None
                ),
                "snapshot_age_minutes": snapshot_age_minutes,
                "estimated_edge": ranking.estimated_edge,
                "opportunity_score": ranking.opportunity_score,
                "best_side": ranking.best_side,
                "best_price": ranking.best_price,
                "fresh": fresh,
                "executable": executable,
                "positive_edge": edge > 0,
                "selection_tier": (
                    "FRESH_EXECUTABLE_POSITIVE_EDGE"
                    if fresh and executable and edge > 0
                    else "RANKED_ACTIVE_FALLBACK"
                ),
                "_sort": (
                    int(fresh and executable and edge > 0),
                    int(executable and edge > 0),
                    edge,
                    _decimal(ranking.opportunity_score),
                    _aware(ranking.ranked_at).timestamp(),
                ),
            }
        )

    candidates.sort(key=lambda row: row["_sort"], reverse=True)
    selected: list[dict[str, Any]] = []
    per_series: Counter[str] = Counter()
    for row in candidates:
        series_key = str(row.get("series_ticker") or "UNKNOWN")
        if per_series[series_key] >= max_per_series:
            continue
        row.pop("_sort", None)
        selected.append(row)
        per_series[series_key] += 1
        if len(selected) >= limit:
            break
    return selected


def run_gh2_single_writer_decision_refresh(
    *,
    session_factory: Callable[[], Session],
    output_dir: Path = Path("reports/phase_gh2"),
    reports_dir: Path = Path("reports"),
    crypto_staging_dir: Path = Path("reports/phase_gh2/crypto_staging"),
    gh1_staging_dir: Path | None = None,
    candidate_manifest_path: Path = Path("reports/phase_gh1/watch/actionable_tickers.json"),
    settings: Settings | None = None,
    candidate_limit: int = 40,
    active_link_limit: int = 250,
    forecast_limit: int = 250,
    opportunity_limit: int = 100,
    freshness_minutes: int = 15,
    soak_cycles_required: int = 24,
    guard_active_writer: bool = True,
    writer_monitor_fn: Callable[[], dict[str, Any]] | None = None,
) -> GH2Artifacts:
    """Run one bounded paper-only decision refresh under a single writer owner."""

    output_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "gh2_active_candidate_refresh.json"
    markdown_path = output_dir / "gh2_active_candidate_refresh.md"
    history_path = output_dir / "gh2_paper_only_soak_history.jsonl"
    resolved = (settings or get_settings()).model_copy(
        update={
            "execution_enabled": False,
            "execution_dry_run": True,
            "autopilot_enabled": False,
            "autopilot_dry_run": True,
        }
    )
    monitor = (writer_monitor_fn or (lambda: db_writer_monitor(settings=resolved)))()
    if guard_active_writer and not bool(monitor.get("safe_to_start_write", True)):
        payload = _blocked_payload(monitor)
        _write_cycle_artifacts(json_path, markdown_path, payload)
        return GH2Artifacts(
            output_dir, json_path, markdown_path, history_path, candidate_manifest_path
        )

    stage_errors: list[str] = []
    websocket_drain = drain_staged_websocket_orderbooks(
        session_factory=session_factory,
        staging_dir=gh1_staging_dir or Path(resolved.kalshi_websocket_staging_dir),
        settings=resolved,
        writer_monitor_fn=lambda: {"safe_to_start_write": True},
    )
    stage_errors.extend(str(item) for item in websocket_drain.get("errors") or [])

    with session_factory() as session:
        paper_orders_before = _paper_order_count(session)
        candidates_before = select_actionable_ranked_markets(
            session,
            limit=candidate_limit,
            freshness_minutes=freshness_minutes,
        )
        active_crypto = _active_market_tickers(
            session,
            prefixes=CRYPTO_TICKER_PREFIXES,
            limit=active_link_limit,
        )
        active_weather = _active_market_tickers(
            session,
            prefixes=WEATHER_TICKER_PREFIXES,
            limit=active_link_limit,
        )
        crypto_drain = drain_staged_crypto_quotes(
            session,
            staging_dir=crypto_staging_dir,
            build_features_after_drain=True,
            link_crypto_after_drain=False,
        )
        stage_errors.extend(str(item) for item in crypto_drain.get("errors") or [])
        ranked_crypto = [row["ticker"] for row in candidates_before if row["model"] == "crypto_v2"]
        ranked_weather = [
            row["ticker"] for row in candidates_before if row["model"] == "weather_v2"
        ]
        crypto_link_tickers = _bounded_unique(
            ranked_crypto + active_crypto,
            active_link_limit,
        )
        weather_link_tickers = _bounded_unique(
            ranked_weather + active_weather,
            active_link_limit,
        )
        crypto_link = link_crypto_markets(
            session,
            tickers=crypto_link_tickers,
            limit=active_link_limit,
        )
        weather_link = link_weather_markets(
            session,
            tickers=weather_link_tickers,
            limit=active_link_limit,
        )

        weather_features = _build_current_weather_features(
            session,
            weather_link_tickers,
            settings=resolved,
        )
        weather_snapshots = (
            latest_snapshots_for_model(
                session,
                model_name="weather_v2",
                limit=forecast_limit,
            )
            or []
        )
        weather_forecasts = run_forecast_models(
            session,
            model_name="weather_v2",
            snapshots=weather_snapshots,
        )
        weather_opportunities = scan_opportunities(
            session,
            model_name="weather_v2",
            limit=opportunity_limit,
            settings=resolved,
            ticker_scope=[snapshot.ticker for snapshot in weather_snapshots],
            scan_mode="GH2_CURRENT_PAPER_ONLY_REFRESH",
        )

        r5_artifacts = write_phase3bc_r5_crypto_freshness_watch_report(
            session,
            output_dir=reports_dir / "phase3bc_r5",
            phase3bc_output_dir=reports_dir / "phase3bc",
            phase3bc_r3_output_dir=reports_dir / "phase3bc_r3",
            phase3bc_r4_output_dir=reports_dir / "phase3bc_r4",
            phase3bc_r7_output_dir=reports_dir / "phase3bc_r7",
            settings=resolved,
            refresh_open_markets=False,
            external_crypto_ingest=False,
            repair_snapshots=False,
            forecast_current_windows_only=True,
            generate_opportunity_report=True,
            crypto_market_scan_limit=active_link_limit,
            crypto_link_limit=active_link_limit,
            forecast_limit=forecast_limit,
            opportunity_limit=opportunity_limit,
            phase3bc_limit=forecast_limit,
            freshness_minutes=freshness_minutes,
            ranking_repair=True,
            ranking_repair_limit=opportunity_limit,
            exact_snapshot_refresh=False,
            near_money_only=False,
        )
        weather_gate = build_phase3ba_r3_weather_paper_gate(
            session,
            output_dir=reports_dir / "phase3ba_r3",
            reports_dir=reports_dir,
            settings=resolved,
            limit=forecast_limit,
            current_window_lookback_hours=3,
        )
        candidates_after = select_actionable_ranked_markets(
            session,
            limit=candidate_limit,
            freshness_minutes=freshness_minutes,
        )
        _write_candidate_manifest(candidate_manifest_path, candidates_after)
        paper_orders_after = _paper_order_count(session)
        session.commit()

    crypto_drain["files_archived"] = _archive_drained_files(
        [Path(path) for path in crypto_drain.get("drained_files") or []],
        archive_dir=crypto_staging_dir / "drained",
    )
    r5_payload = _read_json(r5_artifacts.json_path)
    r5_summary = r5_payload.get("latest_summary") or r5_payload.get("summary") or {}
    weather_summary = weather_gate.get("summary") or {}
    crypto_paper_ready = int(r5_summary.get("paper_ready_candidates") or 0)
    weather_paper_ready = int(weather_summary.get("paper_ready_rows") or 0)
    rankings_inserted = int(weather_opportunities.rankings_inserted) + int(
        (r5_payload.get("latest_summary") or {}).get("ranking_rows") or 0
    )
    if rankings_inserted == 0:
        rankings_inserted = sum(
            1
            for row in candidates_after
            if _aware(datetime.fromisoformat(row["ranked_at"]))
            >= utc_now() - timedelta(minutes=freshness_minutes)
        )
    fresh_candidate_count = sum(
        1
        for row in candidates_after
        if row.get("fresh")
        and _aware(datetime.fromisoformat(row["ranked_at"]))
        >= utc_now() - timedelta(minutes=freshness_minutes)
    )
    paper_orders_created = paper_orders_after - paper_orders_before
    cycle_healthy = (
        not stage_errors
        and rankings_inserted > 0
        and fresh_candidate_count > 0
        and paper_orders_created == 0
    )
    soak = _record_soak_cycle(
        history_path,
        healthy=cycle_healthy,
        paper_ready_candidates=crypto_paper_ready + weather_paper_ready,
        rankings_inserted=rankings_inserted,
        required_cycles=soak_cycles_required,
    )
    payload = {
        "phase": "GH-2",
        "phase_version": PHASE_GH2_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": "PAPER_ONLY_SOAK_RUNNING" if cycle_healthy else "CYCLE_NEEDS_ATTENTION",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "writer_monitor_at_start": monitor,
        "websocket_drain": websocket_drain,
        "crypto_quote_drain": crypto_drain,
        "active_linking": {
            "crypto_candidates": len(active_crypto),
            "weather_candidates": len(active_weather),
            "crypto": asdict(crypto_link),
            "weather": asdict(weather_link),
        },
        "decision_refresh": {
            "weather_features": weather_features,
            "weather_forecasts": asdict(weather_forecasts),
            "weather_rankings_inserted": weather_opportunities.rankings_inserted,
            "weather_opportunities_detected": weather_opportunities.opportunities_detected,
            "crypto_r5_report": str(r5_artifacts.json_path),
            "rankings_inserted_or_fresh": rankings_inserted,
            "fresh_ranked_candidates": fresh_candidate_count,
        },
        "candidate_alignment": {
            "before_count": len(candidates_before),
            "after_count": len(candidates_after),
            "manifest_path": str(candidate_manifest_path),
            "tickers": [row["ticker"] for row in candidates_after],
        },
        "paper_readiness": {
            "crypto_paper_ready_candidates": crypto_paper_ready,
            "crypto_positive_ev_rows": int(r5_summary.get("positive_ev_rows") or 0),
            "weather_paper_ready_candidates": weather_paper_ready,
            "weather_positive_ev_rows": int(
                weather_summary.get("positive_executable_ev_rows") or 0
            ),
            "total_paper_ready_candidates": crypto_paper_ready + weather_paper_ready,
        },
        "soak": soak,
        "errors": stage_errors,
        "safety": {
            "paper_orders_before": paper_orders_before,
            "paper_orders_after": paper_orders_after,
            "paper_orders_created": paper_orders_created,
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "autopilot_enabled": False,
            "explicit_operator_approval_required_after_soak": True,
        },
    }
    _write_cycle_artifacts(json_path, markdown_path, payload)
    return GH2Artifacts(output_dir, json_path, markdown_path, history_path, candidate_manifest_path)


def _active_market_tickers(
    session: Session,
    *,
    prefixes: tuple[str, ...],
    limit: int,
) -> list[str]:
    now = utc_now()
    filters = [
        or_(
            Market.ticker.like(f"{prefix}%"),
            Market.series_ticker.like(f"{prefix}%"),
        )
        for prefix in prefixes
    ]
    statement = (
        select(Market.ticker)
        .where(
            func.lower(func.coalesce(Market.status, "")).in_(("active", "open")),
            or_(Market.close_time.is_(None), Market.close_time > now),
            or_(*filters),
        )
        .order_by(Market.close_time.is_(None), Market.close_time, desc(Market.last_seen_at))
        .limit(limit)
    )
    return list(session.scalars(statement))


def _build_current_weather_features(
    session: Session,
    tickers: list[str],
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    if not tickers:
        return []
    locations = list(
        session.scalars(
            select(WeatherMarketLink.location_key)
            .where(WeatherMarketLink.ticker.in_(tickers))
            .distinct()
            .limit(25)
        )
    )
    summaries = []
    for location in locations:
        summary = build_weather_features(
            session,
            location_key=location,
            settings=settings,
            limit=24,
        )
        summaries.append(asdict(summary))
    return summaries


def _latest_snapshots(
    session: Session,
    tickers: list[str],
) -> dict[str, MarketSnapshot]:
    if not tickers:
        return {}
    statement = (
        select(
            MarketSnapshot,
            func.row_number()
            .over(
                partition_by=MarketSnapshot.ticker,
                order_by=(desc(MarketSnapshot.captured_at), desc(MarketSnapshot.id)),
            )
            .label("row_number"),
        )
        .where(MarketSnapshot.ticker.in_(tickers))
        .subquery()
    )
    snapshot = aliased(MarketSnapshot, statement)
    return {
        row.ticker: row
        for row in session.scalars(select(snapshot).where(statement.c.row_number == 1))
    }


def _paper_order_count(session: Session) -> int:
    return int(session.scalar(select(func.count(PaperOrder.id))) or 0)


def _archive_drained_files(files: list[Path], *, archive_dir: Path) -> int:
    archived = 0
    for path in files:
        if not path.exists():
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / path.name
        path.replace(destination)
        archived += 1
    return archived


def _write_candidate_manifest(path: Path, candidates: list[dict[str, Any]]) -> None:
    _write_json(
        path,
        {
            "phase": "GH-2",
            "generated_at": utc_now().isoformat(),
            "selection": "CURRENT_ACTIONABLE_RANKINGS",
            "tickers": [row["ticker"] for row in candidates],
            "candidates": candidates,
            "paper_only_safety": PAPER_ONLY_SAFETY,
        },
    )


def _record_soak_cycle(
    path: Path,
    *,
    healthy: bool,
    paper_ready_candidates: int,
    rankings_inserted: int,
    required_cycles: int,
) -> dict[str, Any]:
    history = _read_json_lines(path)[-95:]
    history.append(
        {
            "generated_at": utc_now().isoformat(),
            "healthy": healthy,
            "paper_ready_candidates": paper_ready_candidates,
            "rankings_inserted": rankings_inserted,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in history),
        encoding="utf-8",
    )
    temporary.replace(path)
    consecutive = 0
    for row in reversed(history):
        if not row.get("healthy"):
            break
        consecutive += 1
    window = history[-required_cycles:] if required_cycles > 0 else history
    has_candidate = any(int(row.get("paper_ready_candidates") or 0) > 0 for row in window)
    complete = consecutive >= required_cycles and has_candidate
    return {
        "healthy_cycle": healthy,
        "consecutive_healthy_cycles": consecutive,
        "required_healthy_cycles": required_cycles,
        "paper_ready_seen_in_required_window": has_candidate,
        "soak_complete": complete,
        "paper_order_creation_enabled": False,
    }


def _blocked_payload(monitor: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": "GH-2",
        "phase_version": PHASE_GH2_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": "BLOCKED_ACTIVE_WRITER",
        "writer_monitor_at_start": monitor,
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "errors": ["Active writer detected; GH-2 did not open SQLite."],
        "safety": {
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "orders_created": 0,
        },
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    readiness = payload.get("paper_readiness") or {}
    soak = payload.get("soak") or {}
    safety = payload.get("safety") or {}
    return "\n".join(
        [
            "# GH-2 Active Candidate Alignment and Decision Refresh",
            "",
            f"- Status: `{payload.get('status')}`",
            f"- Generated: `{payload.get('generated_at')}`",
            f"- Paper-ready candidates: `{readiness.get('total_paper_ready_candidates', 0)}`",
            f"- Consecutive healthy soak cycles: `{soak.get('consecutive_healthy_cycles', 0)}`",
            f"- Soak complete: `{soak.get('soak_complete', False)}`",
            f"- Paper orders created: `{safety.get('paper_orders_created', 0)}`",
            "- Paper-order creation: `DISABLED`",
            "- Live execution: `DISABLED`",
            "",
        ]
    )


def _write_cycle_artifacts(path: Path, markdown_path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _parse_csv(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))


def _bounded_unique(values: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:limit]


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
