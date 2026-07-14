import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.linker import CryptoLinkResult, link_crypto_markets
from kalshi_predictor.data.schema import (
    CryptoMarketLink,
    SportsFeature,
    SportsGame,
    SportsMarketLink,
    WeatherMarketLink,
)
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.learning.runner import LearningCycleResult, run_learning_once
from kalshi_predictor.learning.safety import (
    learning_daily_cap_status,
    settled_paper_trade_count,
)
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.phase3au import (
    DEFAULT_HEARTBEAT_DIR,
    LongJobHeartbeat,
    deadline_reached,
    stop_after_deadline,
)
from kalshi_predictor.sports.derived_schedule import (
    SportsDerivedScheduleSummary,
    derive_sports_schedule_from_market_legs,
)
from kalshi_predictor.sports.linker import SportsLinkSummary, link_sports_markets
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.linker import WeatherLinkResult, link_weather_markets


@dataclass(frozen=True)
class LinkRemediationResult:
    crypto: CryptoLinkResult
    weather: WeatherLinkResult
    sports: SportsLinkSummary
    sports_derived: SportsDerivedScheduleSummary
    total_links: dict[str, int]
    recommendations: list[str]
    heartbeat_path: str | None = None
    checkpoint_path: str | None = None
    stopped_early: bool = False


@dataclass(frozen=True)
class SettlementWatchCycle:
    cycle_number: int
    settlements_synced: int
    settled_before: int
    settled_after: int
    daily_trades: int
    daily_cap: int
    learning_action: str
    learning_result: LearningCycleResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class SettlementWatchResult:
    status: str
    cycles: list[SettlementWatchCycle]
    stop_reason: str | None
    mode: str = "PAPER ONLY"

    @property
    def settlements_synced(self) -> int:
        return sum(cycle.settlements_synced for cycle in self.cycles)

    @property
    def learning_cycles_started(self) -> int:
        return sum(1 for cycle in self.cycles if cycle.learning_result is not None)

    @property
    def skipped_due_to_cap(self) -> int:
        return sum(1 for cycle in self.cycles if cycle.learning_action == "SKIPPED_DAILY_CAP")

    @property
    def settled_paper_trades(self) -> int:
        if not self.cycles:
            return 0
        return self.cycles[-1].settled_after


SyncSettlementsJob = Callable[[Session, Settings], int]
PnlJob = Callable[[Session, Settings], Any]
LearningJob = Callable[[Session, Settings], LearningCycleResult]
Sleeper = Callable[[float], None]


@dataclass
class SettlementWatchJobs:
    sync_settlements: SyncSettlementsJob = field(default_factory=lambda: _sync_settlements_job)
    paper_pnl: PnlJob = field(default_factory=lambda: _paper_pnl_job)
    learning_once: LearningJob = field(default_factory=lambda: _learning_once_job)


def run_link_remediation(
    session: Session,
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    resume: bool = False,
    heartbeat_dir: Path | None = None,
    progress_every: int = 100,
    checkpoint_every: int = 100,
    stop_after_minutes: int | None = None,
    commit_between_stages: bool = False,
) -> LinkRemediationResult:
    resolved = settings or get_settings()
    heartbeat = LongJobHeartbeat(
        "link-remediate",
        output_dir=heartbeat_dir or DEFAULT_HEARTBEAT_DIR,
        checkpoint_every=checkpoint_every,
    )
    deadline = stop_after_deadline(stop_after_minutes)
    heartbeat.emit(
        stage="STARTING",
        message="Starting Phase 3Y/3AU link remediation.",
        force_checkpoint=True,
        extra={"limit": limit, "resume": resume, "stop_after_minutes": stop_after_minutes},
    )

    crypto = CryptoLinkResult(0, 0, 0, 0, 0)
    weather = WeatherLinkResult(0, 0, {}, {}, 0)
    sports = SportsLinkSummary("ALL", 0, 0, 0)
    sports_derived = SportsDerivedScheduleSummary(0, 0, 0, 0, 0, 0, 0, 0, 0)
    stopped_early = False

    def progress(event: dict[str, object]) -> None:
        processed = int(event.get("processed") or 0)
        if (
            commit_between_stages
            and checkpoint_every > 0
            and processed > 0
            and processed % checkpoint_every == 0
        ):
            session.commit()
        heartbeat.emit(
            stage=str(event.get("stage") or "PROGRESS").upper(),
            processed=processed,
            total=int(event.get("total") or 0),
            current_item=str(event.get("ticker") or ""),
            message=str(event.get("status") or "Progress"),
            extra=event,
        )

    def should_stop() -> bool:
        return deadline_reached(deadline)

    if not should_stop():
        heartbeat.emit(stage="CRYPTO_LINK_START", message="Linking crypto markets.")
        crypto = link_crypto_markets(
            session,
            limit=limit,
            progress_callback=progress,
            progress_every=progress_every,
            should_stop=should_stop,
        )
        _commit_if_requested(session, commit_between_stages)
        heartbeat.emit(
            stage="CRYPTO_LINK_COMPLETE",
            message=f"Crypto link remediation complete: created {crypto.links_created}.",
            force_checkpoint=True,
            extra={
                "links_created": crypto.links_created,
                "markets_scanned": crypto.markets_scanned,
            },
        )

    if should_stop():
        stopped_early = True
    else:
        heartbeat.emit(stage="WEATHER_LINK_START", message="Linking weather markets.")
        weather = link_weather_markets(
            session,
            limit=limit,
            progress_callback=progress,
            progress_every=progress_every,
            should_stop=should_stop,
        )
        _commit_if_requested(session, commit_between_stages)
        heartbeat.emit(
            stage="WEATHER_LINK_COMPLETE",
            message=f"Weather link remediation complete: created {weather.links_created}.",
            force_checkpoint=True,
            extra={
                "links_created": weather.links_created,
                "markets_scanned": weather.markets_scanned,
            },
        )

    if should_stop():
        stopped_early = True
    else:
        heartbeat.emit(stage="SPORTS_LINK_START", message="Linking sports markets.")
        sports = link_sports_markets(
            session,
            league="ALL",
            settings=resolved,
            limit=limit,
            progress_callback=progress,
            progress_every=progress_every,
            should_stop=should_stop,
        )
        _commit_if_requested(session, commit_between_stages)
        heartbeat.emit(
            stage="SPORTS_LINK_COMPLETE",
            message=f"Sports link remediation complete: created {sports.links_created}.",
            force_checkpoint=True,
            extra={
                "links_created": sports.links_created,
                "market_derived": sports.market_derived_links,
                "markets_scanned": sports.markets_scanned,
            },
        )

    if should_stop() or sports.stopped_early:
        stopped_early = True
    else:
        heartbeat.emit(
            stage="SPORTS_DERIVED_START",
            message="Deriving sports schedule/features from parsed legs.",
        )
        sports_derived = derive_sports_schedule_from_market_legs(
            session,
            limit=limit,
            build_features=True,
            settings=resolved,
            progress_callback=progress,
            progress_every=progress_every,
            should_stop=should_stop,
        )
        _commit_if_requested(session, commit_between_stages)
        heartbeat.emit(
            stage="SPORTS_DERIVED_COMPLETE",
            message=(
                "Sports derived schedule complete: "
                f"links={sports_derived.links_created}, "
                f"features={sports_derived.features_created}."
            ),
            force_checkpoint=True,
            extra={
                "links_created": sports_derived.links_created,
                "features_created": sports_derived.features_created,
                "markets_scanned": sports_derived.markets_scanned,
            },
        )

    if sports_derived.stopped_early:
        stopped_early = True
    totals = {
        "crypto": _count(session, CryptoMarketLink),
        "weather": _count(session, WeatherMarketLink),
        "sports": _count(session, SportsMarketLink),
    }
    heartbeat.emit(
        stage="STOPPED_EARLY" if stopped_early else "COMPLETE",
        message=(
            "Stopped early by --stop-after-minutes; rerun with --resume."
            if stopped_early
            else "Link remediation completed."
        ),
        force_checkpoint=True,
        extra={"total_links": totals},
    )
    return LinkRemediationResult(
        crypto=crypto,
        weather=weather,
        sports=sports,
        sports_derived=sports_derived,
        total_links=totals,
        recommendations=_link_recommendations(crypto, weather, sports, sports_derived, totals),
        heartbeat_path=str(heartbeat.heartbeat_path),
        checkpoint_path=str(heartbeat.checkpoint_path),
        stopped_early=stopped_early,
    )


def run_settlement_watcher(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    jobs: SettlementWatchJobs | None = None,
    cycles: int = 24,
    interval_minutes: int = 15,
    resume_learning_after_cap_reset: bool = True,
    lowered_min_score: Decimal = Decimal("25"),
    lowered_min_edge: Decimal = Decimal("0.01"),
    scan_limit: int = 500,
    sleeper: Sleeper = time.sleep,
) -> SettlementWatchResult:
    resolved = settings or get_settings()
    resolved_jobs = jobs or SettlementWatchJobs()
    cycle_results: list[SettlementWatchCycle] = []
    status = "COMPLETED"
    stop_reason: str | None = None

    try:
        for cycle_number in range(1, max(0, cycles) + 1):
            with session_factory() as session:
                cycle = _run_watch_cycle(
                    session,
                    settings=resolved,
                    jobs=resolved_jobs,
                    cycle_number=cycle_number,
                    resume_learning_after_cap_reset=resume_learning_after_cap_reset,
                    lowered_min_score=lowered_min_score,
                    lowered_min_edge=lowered_min_edge,
                    scan_limit=scan_limit,
                )
                cycle_results.append(cycle)
                session.commit()
                if cycle.error:
                    status = "COMPLETED_WITH_ERRORS"
            if cycle_number < cycles:
                sleeper(max(0, interval_minutes * 60))
        stop_reason = f"Reached max cycles ({cycles})."
    except KeyboardInterrupt:
        status = "INTERRUPTED"
        stop_reason = "Interrupted by user."
    return SettlementWatchResult(status=status, cycles=cycle_results, stop_reason=stop_reason)


def phase3y_learning_resume_settings(
    settings: Settings,
    *,
    lowered_min_score: Decimal = Decimal("25"),
    lowered_min_edge: Decimal = Decimal("0.01"),
    scan_limit: int = 500,
) -> Settings:
    return settings.model_copy(
        update={
            "learning_mode": True,
            "learning_min_edge": lowered_min_edge,
            "learning_min_opportunity_score": lowered_min_score,
            "learning_candidate_scan_limit": scan_limit,
            "learning_prioritize_fast_settlement": True,
            "learning_max_days_to_settlement": 1,
            "learning_block_demo_execution": True,
            "learning_block_live_execution": True,
            "execution_enabled": False,
            "execution_dry_run": True,
        }
    )


def generate_phase3y_report(
    session: Session,
    *,
    output_path: Path = Path("reports/phase3y_report.md"),
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    cap = learning_daily_cap_status(session, settings=resolved)
    settled = settled_paper_trade_count(session)
    crypto_links = _count(session, CryptoMarketLink)
    weather_links = _count(session, WeatherMarketLink)
    sports_links = _count(session, SportsMarketLink)
    sports_games = _count(session, SportsGame)
    sports_features = _count(session, SportsFeature)
    lines = [
        "# Phase 3Y Settlement & Link Remediation",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER ONLY",
        "- Demo/live execution: blocked; this phase does not place live orders.",
        "",
        "## Settlement Watch",
        "",
        f"- Settled paper trades: {settled} / {resolved.learning_target_settled_trades}",
        f"- Daily paper trades: {cap['daily_trades']} / {cap['daily_cap']}",
        f"- Daily cap reached: {cap['reached']}",
        "",
        "## Link Readiness",
        "",
        f"- Crypto links: {crypto_links}",
        f"- Weather links: {weather_links}",
        f"- Sports links: {sports_links}",
        f"- Sports games: {sports_games}",
        f"- Sports features: {sports_features}",
        "",
        "## Learning Resume Policy",
        "",
        "- If the daily cap is reached, settlement-watch syncs settlements and reports only.",
        "- After the UTC daily cap resets, settlement-watch may run one paper-only learning cycle.",
        "- Resume thresholds: min score 25, min edge 0.01, candidate scan limit 500.",
        "",
        "## Recommended Next Commands",
        "",
        "```bash",
        "kalshi-bot paper-settlement-doctor --output-dir reports/paper_settlement_reconciliation",
        "kalshi-bot link-remediate",
        "kalshi-bot settlement-watch --cycles 8 --interval-minutes 15",
        "kalshi-bot learning-diagnostics --scan-limit 500 --suggest-thresholds",
        "```",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def _run_watch_cycle(
    session: Session,
    *,
    settings: Settings,
    jobs: SettlementWatchJobs,
    cycle_number: int,
    resume_learning_after_cap_reset: bool,
    lowered_min_score: Decimal,
    lowered_min_edge: Decimal,
    scan_limit: int,
) -> SettlementWatchCycle:
    settled_before = settled_paper_trade_count(session)
    cap = learning_daily_cap_status(session, settings=settings)
    learning_action = "SKIPPED_DISABLED"
    learning_result: LearningCycleResult | None = None
    error: str | None = None
    settlements = 0
    try:
        settlements = jobs.sync_settlements(session, settings)
        jobs.paper_pnl(session, settings)
        if not settings.learning_mode:
            learning_action = "SKIPPED_LEARNING_MODE_OFF"
        elif cap["reached"]:
            learning_action = "SKIPPED_DAILY_CAP"
        elif resume_learning_after_cap_reset:
            learning_settings = phase3y_learning_resume_settings(
                settings,
                lowered_min_score=lowered_min_score,
                lowered_min_edge=lowered_min_edge,
                scan_limit=scan_limit,
            )
            learning_result = jobs.learning_once(session, learning_settings)
            learning_action = "RAN_LOWER_THRESHOLDS"
        else:
            learning_action = "SKIPPED_BY_OPTION"
    except Exception as exc:  # pragma: no cover - exact failure source is environment dependent.
        session.rollback()
        error = f"{type(exc).__name__}: {exc}"
        learning_action = "ERROR"
    return SettlementWatchCycle(
        cycle_number=cycle_number,
        settlements_synced=settlements,
        settled_before=settled_before,
        settled_after=settled_paper_trade_count(session),
        daily_trades=int(cap["daily_trades"]),
        daily_cap=int(cap["daily_cap"]),
        learning_action=learning_action,
        learning_result=learning_result,
        error=error,
    )


def _sync_settlements_job(session: Session, settings: Settings) -> int:
    del settings
    return sync_settlements(lookback_days=30, limit=100, max_pages=5, session=session)


def _paper_pnl_job(session: Session, settings: Settings) -> Any:
    del settings
    return calculate_and_store_pnl(session)


def _learning_once_job(session: Session, settings: Settings) -> LearningCycleResult:
    return run_learning_once(session, settings=settings)


def _count(session: Session, table: Any) -> int:
    return int(session.scalar(select(func.count()).select_from(table)) or 0)


def _commit_if_requested(session: Session, commit_between_stages: bool) -> None:
    if commit_between_stages:
        session.commit()


def _link_recommendations(
    crypto: CryptoLinkResult,
    weather: WeatherLinkResult,
    sports: SportsLinkSummary,
    sports_derived: SportsDerivedScheduleSummary,
    totals: dict[str, int],
) -> list[str]:
    recommendations: list[str] = []
    if totals["crypto"] == 0:
        recommendations.append(
            f"Crypto data exists only if matching {DEFAULT_CRYPTO_SYMBOLS} markets are scanned; "
            "run collect-once across more open market pages, then link-crypto-markets."
        )
    if totals["weather"] == 0:
        recommendations.append(
            "Weather features need matching weather markets; collect more open markets and "
            "verify location/metric terms appear in market title, ticker, or rules."
        )
    if sports_derived.links_created > 0 or sports_derived.features_created > 0:
        recommendations.append(
            "Sports markets were repaired with Kalshi-event-derived teams/games/features. "
            "Use ingest-sports later to upgrade these links with external schedule evidence."
        )
    elif sports.games_scanned == 0:
        recommendations.append(
            "Sports links need parsed market legs or ingested schedule/team data. Run "
            "market-legs-parse --refresh, then derive-sports-schedule or ingest-sports."
        )
    elif totals["sports"] == 0:
        recommendations.append(
            "Sports games exist but did not match markets. Lower SPORTS_MIN_LINK_CONFIDENCE "
            "only for diagnostics, or add team aliases to sports ingestion."
        )
    if not recommendations:
        recommendations.append(
            "Link remediation found matching data. Rebuild features and rerun forecasts."
        )
    recommendations.append(
        f"Last remediation matched crypto={crypto.links_created}, "
        f"weather={weather.links_created}, sports={sports.links_created}, "
        f"sports_derived={sports_derived.links_created}."
    )
    return recommendations
