import importlib.util
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from rich.console import Console
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kalshi_predictor.autopilot.reports import build_autopilot_status
from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.crypto.assets import DEFAULT_CRYPTO_SYMBOLS
from kalshi_predictor.crypto.features import build_crypto_features
from kalshi_predictor.crypto.ingestion import ingest_crypto_quotes
from kalshi_predictor.crypto.linker import link_crypto_markets
from kalshi_predictor.crypto.repository import parse_symbols
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    require_postgres_for_overnight_if_configured,
)
from kalshi_predictor.data.db import describe_db_location
from kalshi_predictor.data.maintenance import database_status_card
from kalshi_predictor.data.repositories import get_recent_snapshots
from kalshi_predictor.data.schema import (
    CryptoFeature,
    CryptoMarketLink,
    CryptoPrice,
    Forecast,
    MarketOpportunity,
    WeatherFeature,
    WeatherForecast,
    WeatherMarketLink,
)
from kalshi_predictor.forecasting.registry import run_forecast_models
from kalshi_predictor.forecasting.status import model_status_summary
from kalshi_predictor.ingest.markets import sync_settlements
from kalshi_predictor.jobs.collect_once import collect_once
from kalshi_predictor.leaderboard.reports import generate_leaderboard_report
from kalshi_predictor.learning.reports import generate_learning_report
from kalshi_predictor.learning.runner import run_learning_once
from kalshi_predictor.learning.safety import (
    learning_blocks_demo_execution,
    learning_blocks_live_execution,
    learning_daily_cap_status,
    learning_status,
)
from kalshi_predictor.overnight.reports import build_overnight_status, generate_overnight_report
from kalshi_predictor.paper.ledger import get_paper_summary
from kalshi_predictor.paper.pnl import calculate_and_store_pnl
from kalshi_predictor.phase3y import phase3y_learning_resume_settings
from kalshi_predictor.signals.reports import generate_signal_report
from kalshi_predictor.signals.status import signal_status_summary
from kalshi_predictor.utils.time import utc_now
from kalshi_predictor.weather.features import build_weather_features
from kalshi_predictor.weather.ingestion import ingest_weather_location
from kalshi_predictor.weather.linker import link_weather_markets

READY = "READY"
WARNING = "WARNING"
BLOCKED = "BLOCKED"
KANSAS_CITY_LAT = 39.0997
KANSAS_CITY_LON = -94.5786
RECOVERY_INSTRUCTIONS = (
    "Stop other bot/UI processes, close DB viewers, move the SQLite DB out of OneDrive, "
    "restore from backup if integrity_check reports malformed, then rerun kalshi-bot "
    "tonight-check."
)

StepJob = Callable[[Session, Settings], Mapping[str, Any]]


@dataclass(frozen=True)
class TonightCheckItem:
    name: str
    status: str
    message: str


@dataclass(frozen=True)
class TonightCheckResult:
    status: str
    items: list[TonightCheckItem]
    summary: dict[str, Any]
    recovery_instructions: str = ""

    @property
    def warnings(self) -> list[TonightCheckItem]:
        return [item for item in self.items if item.status == WARNING]

    @property
    def blocked(self) -> list[TonightCheckItem]:
        return [item for item in self.items if item.status == BLOCKED]


@dataclass(frozen=True)
class TonightStepResult:
    cycle: int
    name: str
    status: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class TonightRunResult:
    status: str
    check: TonightCheckResult
    cycles_completed: int
    stop_reason: str | None
    steps: list[TonightStepResult]

    @property
    def errors(self) -> list[TonightStepResult]:
        return [step for step in self.steps if step.status == "ERROR"]


@dataclass
class TonightJobs:
    collect_once: StepJob = field(default_factory=lambda: _collect_once_step)
    ingest_crypto: StepJob = field(default_factory=lambda: _ingest_crypto_step)
    build_crypto_features: StepJob = field(default_factory=lambda: _build_crypto_features_step)
    link_crypto_markets: StepJob = field(default_factory=lambda: _link_crypto_markets_step)
    ingest_weather: StepJob = field(default_factory=lambda: _ingest_weather_step)
    build_weather_features: StepJob = field(default_factory=lambda: _build_weather_features_step)
    link_weather_markets: StepJob = field(default_factory=lambda: _link_weather_markets_step)
    forecast_all: StepJob = field(default_factory=lambda: _forecast_all_step)
    model_health: StepJob = field(default_factory=lambda: _model_health_step)
    signals_status: StepJob = field(default_factory=lambda: _signals_status_step)
    learning_once: StepJob = field(default_factory=lambda: _learning_once_step)
    paper_pnl: StepJob = field(default_factory=lambda: _paper_pnl_step)
    sync_settlements: StepJob = field(default_factory=lambda: _sync_settlements_step)
    model_confidence: StepJob = field(default_factory=lambda: _model_confidence_step)
    learning_report: StepJob = field(default_factory=lambda: _learning_report_step)
    signals_report: StepJob = field(default_factory=lambda: _signals_report_step)
    paper_summary: StepJob = field(default_factory=lambda: _paper_summary_step)
    leaderboard: StepJob = field(default_factory=lambda: _leaderboard_step)
    overnight_report: StepJob = field(default_factory=lambda: _overnight_report_step)

    def ordered(self) -> list[tuple[str, StepJob]]:
        return [
            ("collect-once", self.collect_once),
            (f"ingest-crypto {DEFAULT_CRYPTO_SYMBOLS}", self.ingest_crypto),
            ("build-crypto-features", self.build_crypto_features),
            ("link-crypto-markets", self.link_crypto_markets),
            ("ingest-weather kansas_city", self.ingest_weather),
            ("build-weather-features", self.build_weather_features),
            ("link-weather-markets", self.link_weather_markets),
            ("forecast --model all", self.forecast_all),
            ("model-health", self.model_health),
            ("signals-status", self.signals_status),
            ("learning-once", self.learning_once),
            ("paper-pnl", self.paper_pnl),
            ("sync-settlements", self.sync_settlements),
            ("model-confidence", self.model_confidence),
            ("learning-report", self.learning_report),
            ("signals-report", self.signals_report),
            ("paper-summary", self.paper_summary),
            ("leaderboard", self.leaderboard),
            ("overnight-report", self.overnight_report),
        ]


def build_tonight_check(
    session: Session,
    *,
    settings: Settings | None = None,
    project_path: Path | None = None,
    reports_dir: Path | None = None,
    check_port: bool = True,
    port: int = 8080,
) -> TonightCheckResult:
    resolved_settings = settings or get_settings()
    resolved_project = Path(project_path or Path.cwd()).resolve()
    resolved_reports = Path(reports_dir or "reports")
    items: list[TonightCheckItem] = []
    summary: dict[str, Any] = {}

    try:
        session.execute(text("SELECT 1")).scalar()
        items.append(TonightCheckItem("DB reachable", READY, "Database connection succeeded."))
        integrity = _sqlite_integrity_check(session)
        items.append(integrity)
        database = database_status_card(session, settings=resolved_settings)
        summary["database"] = database
        items.append(
            TonightCheckItem(
                "DB backend",
                database["status"],
                f"{database['backend']} at {database['location']}",
            )
        )
        try:
            require_postgres_for_overnight_if_configured(resolved_settings)
        except RuntimeError as exc:
            items.append(TonightCheckItem("PostgreSQL requirement", BLOCKED, str(exc)))
    except SQLAlchemyError as exc:
        message = str(exc)
        status = BLOCKED if _db_is_locked_or_malformed(message) else WARNING
        items.append(TonightCheckItem("DB health", status, message))
        return _check_result(items, summary, recovery=RECOVERY_INSTRUCTIONS)

    db_location = _db_location(session, resolved_settings)
    summary["db_location"] = db_location
    if _contains_onedrive(resolved_project) or _contains_onedrive(Path(db_location)):
        items.append(
            TonightCheckItem(
                "OneDrive path",
                WARNING,
                "SQLite on OneDrive can corrupt under concurrent writes. Move to "
                "~/projects for overnight runs.",
            )
        )
    else:
        items.append(TonightCheckItem("OneDrive path", READY, "Project and DB are off OneDrive."))

    if resolved_settings.kalshi_env.lower() in {"live", "prod", "production"}:
        items.append(
            TonightCheckItem(
                "Kalshi environment",
                BLOCKED,
                f"KALSHI_ENV={resolved_settings.kalshi_env}; tonight-run is blocked.",
            )
        )
    else:
        items.append(
            TonightCheckItem(
                "Kalshi environment",
                READY,
                f"KALSHI_ENV={resolved_settings.kalshi_env}; production/live is not active.",
            )
        )

    learning = learning_status(session, settings=resolved_settings)
    overnight = build_overnight_status(session, settings=resolved_settings)
    autopilot = build_autopilot_status(session, settings=resolved_settings)
    models = model_status_summary(session)
    signals = signal_status_summary(session)
    crypto = _crypto_readiness(session)
    weather = _weather_readiness(session)
    latest_forecasts = _recent_count(session, Forecast, Forecast.forecasted_at)
    latest_opportunities = _recent_count(
        session,
        MarketOpportunity,
        MarketOpportunity.detected_at,
    )

    summary.update(
        {
            "learning": learning,
            "overnight_status": overnight["plain_status"],
            "autopilot_status": autopilot["plain_status"],
            "paper_trades_today": learning["paper_trades_created_today"],
            "settled_paper_trades": learning["settled_paper_trades"],
            "target_settled_trades": learning["target_settled_trades"],
            "progress_percent": learning["progress_percent"],
            "latest_forecast_count": latest_forecasts,
            "latest_opportunity_count": latest_opportunities,
            "inactive_model_count": len(models.inactive_models),
            "inactive_signal_count": len(signals.inactive_signals),
            "crypto": crypto,
            "weather": weather,
            "paper_only_confirmed": _paper_only_confirmed(resolved_settings),
        }
    )
    items.extend(
        [
            _ready_if(learning["enabled"], "Learning Mode", "Learning Mode is ON."),
            TonightCheckItem("Overnight status", READY, overnight["plain_status"]),
            TonightCheckItem("Autopilot status", READY, autopilot["plain_status"]),
            _ready_if(
                learning_blocks_demo_execution(resolved_settings),
                "Demo execution blocked",
                "Learning Mode blocks demo execution.",
            ),
            _ready_if(
                learning_blocks_live_execution(resolved_settings),
                "Live execution blocked",
                "Learning Mode blocks live execution.",
            ),
            _warning_if(
                resolved_settings.execution_enabled,
                "Execution enabled",
                "EXECUTION_ENABLED=true; tonight-run will not call execution endpoints.",
                "EXECUTION_ENABLED=false.",
            ),
            _trade_generation_item(learning),
            TonightCheckItem(
                "Paper trades today",
                READY,
                f"{learning['paper_trades_created_today']} / "
                f"{learning['daily_paper_trade_cap']}",
            ),
            TonightCheckItem(
                "Settled paper trades",
                READY,
                f"{learning['settled_paper_trades']} / "
                f"{learning['target_settled_trades']} ({learning['progress_percent']})",
            ),
            TonightCheckItem("Latest forecasts", READY, f"{latest_forecasts} in last 24h."),
            TonightCheckItem(
                "Latest opportunities",
                READY,
                f"{latest_opportunities} in last 24h.",
            ),
            TonightCheckItem(
                "Inactive models",
                READY,
                f"{len(models.inactive_models)} inactive core model(s).",
            ),
            TonightCheckItem(
                "Inactive signals",
                READY,
                f"{len(signals.inactive_signals)} inactive expected signal(s).",
            ),
            _readiness_item("Crypto ingestion readiness", crypto),
            _readiness_item("Weather ingestion readiness", weather),
            _reports_writable_item(resolved_reports),
            _ui_dependency_item(),
        ]
    )
    if check_port:
        items.append(_port_item(port))
    return _check_result(items, summary)


def tonight_card(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    learning = learning_status(session, settings=resolved_settings)
    signals = signal_status_summary(session)
    models = model_status_summary(session)
    paper_only = _paper_only_confirmed(resolved_settings)
    errors = _latest_learning_errors(learning)
    status = READY
    if resolved_settings.kalshi_env.lower() in {"live", "prod", "production"}:
        status = BLOCKED
    elif not paper_only or not learning["enabled"] or errors:
        status = WARNING
    return {
        "status": status,
        "learning_mode": "ON" if learning["enabled"] else "OFF",
        "paper_only_confirmed": paper_only,
        "trades_today": learning["paper_trades_created_today"],
        "settled_progress": (
            f"{learning['settled_paper_trades']} / {learning['target_settled_trades']} "
            f"({learning['progress_percent']})"
        ),
        "last_cycle": learning["latest_cycle_status"],
        "errors": errors,
        "inactive_models": len(models.inactive_models),
        "inactive_signals": len(signals.inactive_signals),
        "report_path": "/reports/tonight_report.md",
    }


def run_tonight(
    session_factory: Callable[[], Session],
    *,
    settings: Settings | None = None,
    jobs: TonightJobs | None = None,
    max_cycles: int = 32,
    interval_minutes: int = 15,
    sleeper: Callable[[float], None] = time.sleep,
    console: Console | None = None,
) -> TonightRunResult:
    resolved_settings = settings or get_settings()
    resolved_jobs = jobs or TonightJobs()
    resolved_console = console or Console()
    with session_factory() as session:
        check = build_tonight_check(session, settings=resolved_settings)
    if check.status == BLOCKED:
        resolved_console.print("tonight-check: BLOCKED")
        return TonightRunResult(
            status=BLOCKED,
            check=check,
            cycles_completed=0,
            stop_reason="tonight-check blocked the run.",
            steps=[],
        )

    steps: list[TonightStepResult] = []
    stop_reason: str | None = None
    cycles_completed = 0
    safe_settings = resolved_settings.model_copy(
        update={
            "learning_mode": True,
            "overnight_run_demo": False,
            "execution_enabled": False,
        }
    )
    try:
        for cycle in range(1, max_cycles + 1):
            resolved_console.print(f"Tonight cycle {cycle}/{max_cycles}")
            for name, job in resolved_jobs.ordered():
                with session_factory() as session:
                    step = _run_step(cycle, name, job, session, safe_settings)
                steps.append(step)
                resolved_console.print(f"- {name}: {step.status} {step.message}")
            cycles_completed += 1
            if cycle < max_cycles:
                _sleep(interval_minutes, sleeper)
    except KeyboardInterrupt:
        stop_reason = "Interrupted by user."
    status = (
        "COMPLETED_WITH_ERRORS"
        if any(step.status == "ERROR" for step in steps)
        else "COMPLETED"
    )
    if stop_reason:
        status = "INTERRUPTED"
    return TonightRunResult(
        status=status,
        check=check,
        cycles_completed=cycles_completed,
        stop_reason=stop_reason,
        steps=steps,
    )


def generate_tonight_report(
    session: Session,
    *,
    output_path: str | Path = Path("reports/tonight_report.md"),
    settings: Settings | None = None,
) -> Path:
    resolved_settings = settings or get_settings()
    check = build_tonight_check(session, settings=resolved_settings, check_port=False)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_tonight_report(check), encoding="utf-8")
    return output


def render_tonight_report(check: TonightCheckResult) -> str:
    learning = check.summary.get("learning") or {}
    crypto = check.summary.get("crypto") or {}
    weather = check.summary.get("weather") or {}
    inactive = [item for item in check.items if item.status != READY]
    lines = [
        "# Tonight Readiness Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        "- Mode: PAPER ONLY",
        "- Demo/live execution: not called by tonight-run",
        "",
        "## Overall Status",
        "",
        f"**{check.status}**",
        "",
        "## Learning Progress",
        "",
        f"- Learning Mode: {'ON' if learning.get('enabled') else 'OFF'}",
        f"- Progress: {learning.get('settled_paper_trades', 0)} / "
        f"{learning.get('target_settled_trades', 500)} "
        f"({learning.get('progress_percent', '0.0%')})",
        f"- Expected completion: {learning.get('expected_completion', 'n/a')}",
        "",
        "## Paper Trades Created",
        "",
        f"- Today: {learning.get('paper_trades_created_today', 0)} / "
        f"{learning.get('daily_paper_trade_cap', 0)}",
        f"- Trade generation health: "
        f"{(learning.get('trade_generation_health') or {}).get('label', 'n/a')}",
        "",
        "## Settled Trades",
        "",
        f"- Settled paper trades: {learning.get('settled_paper_trades', 0)}",
        "",
        "## Model Health",
        "",
        f"- Inactive models: {check.summary.get('inactive_model_count', 0)}",
        "",
        "## Signal Health",
        "",
        f"- Inactive signals: {check.summary.get('inactive_signal_count', 0)}",
        "",
        "## Inactive Models/Signals",
        "",
        f"- Inactive models: {check.summary.get('inactive_model_count', 0)}",
        f"- Inactive signals: {check.summary.get('inactive_signal_count', 0)}",
        "",
        "## Crypto/Weather Readiness",
        "",
        f"- Crypto: {crypto.get('status', 'UNKNOWN')} - {crypto.get('message', 'n/a')}",
        f"- Weather: {weather.get('status', 'UNKNOWN')} - {weather.get('message', 'n/a')}",
        "",
        "## DB Health",
        "",
    ]
    for item in check.items:
        if item.name.startswith("DB"):
            lines.append(f"- {item.name}: {item.status} - {item.message}")
    lines.extend(
        [
            "",
            "## Errors Encountered",
            "",
        ]
    )
    if inactive:
        for item in inactive:
            lines.append(f"- {item.name}: {item.status} - {item.message}")
    else:
        lines.append("- No readiness errors or warnings.")
    lines.extend(
        [
            "",
            "## Recommended Morning Action",
            "",
            _morning_action(check),
            "",
        ]
    )
    return "\n".join(lines)


def render_tonight_check(check: TonightCheckResult) -> str:
    lines = [f"Tonight check: {check.status}", ""]
    for item in check.items:
        lines.append(f"- {item.status}: {item.name} - {item.message}")
    if check.recovery_instructions:
        lines.extend(["", f"Recovery: {check.recovery_instructions}"])
    return "\n".join(lines)


def _check_result(
    items: list[TonightCheckItem],
    summary: dict[str, Any],
    *,
    recovery: str = "",
) -> TonightCheckResult:
    severity = {READY: 0, WARNING: 1, BLOCKED: 2}
    status = max((item.status for item in items), key=lambda value: severity[value])
    return TonightCheckResult(
        status=status,
        items=items,
        summary=summary,
        recovery_instructions=recovery,
    )


def _run_step(
    cycle: int,
    name: str,
    job: StepJob,
    session: Session,
    settings: Settings,
) -> TonightStepResult:
    try:
        details = dict(job(session, settings))
        session.commit()
        return TonightStepResult(cycle, name, "OK", "completed", details)
    except Exception as exc:  # noqa: BLE001 - nightly steps are non-fatal by design.
        session.rollback()
        return TonightStepResult(
            cycle=cycle,
            name=name,
            status="ERROR",
            message=str(exc) or type(exc).__name__,
            error=str(exc) or type(exc).__name__,
        )


def _sqlite_integrity_check(session: Session) -> TonightCheckItem:
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return TonightCheckItem("DB integrity", READY, "Non-SQLite DB; integrity PRAGMA skipped.")
    result = str(session.execute(text("PRAGMA integrity_check")).scalar() or "")
    if result.lower() == "ok":
        return TonightCheckItem("DB integrity", READY, "SQLite integrity_check returned ok.")
    return TonightCheckItem("DB integrity", BLOCKED, f"SQLite integrity_check returned {result}.")


def _db_is_locked_or_malformed(message: str) -> bool:
    normalized = message.lower()
    return "database is locked" in normalized or "malformed" in normalized


def _db_location(session: Session, settings: Settings) -> str:
    bind = session.get_bind()
    database = getattr(bind.url, "database", None)
    if bind.dialect.name == "sqlite" and database:
        return str(Path(database).resolve()) if database != ":memory:" else ":memory:"
    try:
        return describe_db_location(database_url_from_settings(settings))
    except Exception:
        return database_url_from_settings(settings)


def _contains_onedrive(path: Path) -> bool:
    return "onedrive" in str(path).lower()


def _ready_if(condition: bool, name: str, ok_message: str) -> TonightCheckItem:
    message = ok_message if condition else "Needs attention."
    return TonightCheckItem(name, READY if condition else WARNING, message)


def _warning_if(
    condition: bool,
    name: str,
    warning_message: str,
    ok_message: str,
) -> TonightCheckItem:
    message = warning_message if condition else ok_message
    return TonightCheckItem(name, WARNING if condition else READY, message)


def _trade_generation_item(learning: dict[str, Any]) -> TonightCheckItem:
    health = learning.get("trade_generation_health") or {}
    status = WARNING if health.get("kind") == "risk" else READY
    return TonightCheckItem(
        "Paper trade generation",
        status,
        f"{health.get('label', 'Unknown')}: {health.get('message', 'No message.')}",
    )


def _crypto_readiness(session: Session) -> dict[str, Any]:
    prices = _count(session, CryptoPrice)
    features = _count(session, CryptoFeature)
    links = _count(session, CryptoMarketLink)
    ready = prices > 0 and features > 0 and links > 0
    return {
        "status": READY if ready else WARNING,
        "prices": prices,
        "features": features,
        "links": links,
        "message": f"prices={prices}, features={features}, links={links}",
    }


def _weather_readiness(session: Session) -> dict[str, Any]:
    forecasts = _count(session, WeatherForecast)
    features = _count(session, WeatherFeature)
    links = _count(session, WeatherMarketLink)
    ready = forecasts > 0 and features > 0 and links > 0
    return {
        "status": READY if ready else WARNING,
        "forecasts": forecasts,
        "features": features,
        "links": links,
        "message": f"forecasts={forecasts}, features={features}, links={links}",
    }


def _readiness_item(name: str, readiness: dict[str, Any]) -> TonightCheckItem:
    return TonightCheckItem(name, readiness["status"], readiness["message"])


def _reports_writable_item(reports_dir: Path) -> TonightCheckItem:
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        probe = reports_dir / ".tonight_write_check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return TonightCheckItem("Reports directory", READY, f"{reports_dir} is writable.")
    except OSError as exc:
        return TonightCheckItem("Reports directory", BLOCKED, str(exc))


def _ui_dependency_item() -> TonightCheckItem:
    missing = [
        name
        for name in ("fastapi", "uvicorn", "jinja2")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        return TonightCheckItem("UI dependencies", WARNING, f"Missing: {', '.join(missing)}")
    return TonightCheckItem("UI dependencies", READY, "FastAPI, uvicorn, and Jinja2 are installed.")


def _port_item(port: int) -> TonightCheckItem:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        occupied = sock.connect_ex(("127.0.0.1", port)) == 0
    if occupied:
        return TonightCheckItem("Port 8080", WARNING, f"Port {port} is already in use.")
    return TonightCheckItem("Port 8080", READY, f"Port {port} is available.")


def _paper_only_confirmed(settings: Settings) -> bool:
    return (
        settings.learning_mode
        and learning_blocks_demo_execution(settings)
        and learning_blocks_live_execution(settings)
        and not settings.execution_enabled
        and settings.kalshi_env.lower() not in {"live", "prod", "production"}
    )


def _latest_learning_errors(learning: dict[str, Any]) -> int:
    failed_statuses = {"COMPLETED_WITH_ERRORS", "FAILED"}
    return 0 if learning.get("latest_cycle_status") not in failed_statuses else 1


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _recent_count(session: Session, model: type, column: Any) -> int:
    since = utc_now() - timedelta(hours=24)
    return int(session.scalar(select(func.count()).select_from(model).where(column >= since)) or 0)


def _sleep(interval_minutes: int, sleeper: Callable[[float], None]) -> None:
    seconds = max(0, interval_minutes * 60)
    if seconds:
        sleeper(seconds)


def _morning_action(check: TonightCheckResult) -> str:
    if check.status == BLOCKED:
        return "Resolve blocked readiness checks before restarting the overnight loop."
    if check.status == WARNING:
        return (
            "Review warnings, then inspect settled trades, model confidence, signal health, "
            "and P&L."
        )
    return (
        "Review settled trades, model confidence, signal health, and paper P&L before "
        "changing thresholds."
    )


def _collect_once_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = collect_once(
        status="open",
        limit=100,
        max_pages=1,
        include_orderbook=True,
        session=session,
        console=Console(file=None, quiet=True),
    )
    return {
        "markets_seen": summary.markets_seen,
        "snapshots_inserted": summary.snapshots_inserted,
        "forecasts_inserted": summary.forecasts_inserted,
    }


def _ingest_crypto_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = ingest_crypto_quotes(
        session,
        symbols=parse_symbols(DEFAULT_CRYPTO_SYMBOLS),
        source="coinbase",
    )
    return {"prices_inserted": summary.prices_inserted, "errors": summary.errors}


def _build_crypto_features_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = build_crypto_features(session, symbols=parse_symbols(DEFAULT_CRYPTO_SYMBOLS))
    return {
        "features_inserted": summary.features_inserted,
        "symbols_processed": summary.symbols_processed,
    }


def _link_crypto_markets_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = link_crypto_markets(session)
    return {
        "links_created": summary.links_created,
        "markets_scanned": summary.markets_scanned,
        "multi_asset_links": summary.multi_asset_links,
        "links_by_symbol": summary.links_by_symbol,
    }


def _ingest_weather_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = ingest_weather_location(
        session,
        location_key="kansas_city",
        latitude=KANSAS_CITY_LAT,
        longitude=KANSAS_CITY_LON,
    )
    return {"forecasts_inserted": summary.forecasts_inserted, "errors": summary.errors}


def _build_weather_features_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = build_weather_features(session, location_key="kansas_city")
    return {
        "features_inserted": summary.features_inserted,
        "forecasts_processed": summary.forecasts_processed,
    }


def _link_weather_markets_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = link_weather_markets(session)
    return {"links_created": summary.links_created, "markets_scanned": summary.markets_scanned}


def _forecast_all_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    snapshots = get_recent_snapshots(session, limit=100)
    summary = run_forecast_models(session, model_name="all", snapshots=snapshots)
    return {
        "snapshots_scanned": summary.snapshots_scanned,
        "forecasts_inserted": summary.forecasts_inserted,
    }


def _model_health_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = model_status_summary(session)
    return {"inactive_models": len(summary.inactive_models)}


def _signals_status_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = signal_status_summary(session, log_skips=True)
    return {"inactive_signals": len(summary.inactive_signals)}


def _learning_once_step(session: Session, settings: Settings) -> dict[str, Any]:
    cap = learning_daily_cap_status(session, settings=settings)
    if cap["reached"]:
        return {
            "status": "SKIPPED_DAILY_CAP",
            "paper_trades_created": 0,
            "forecasts_generated": 0,
            "opportunities_found": 0,
            "errors": 0,
            "message": cap["message"],
            "next_action": "Settlement sync and reports continue until the UTC daily cap resets.",
        }
    learning_settings = phase3y_learning_resume_settings(settings)
    result = run_learning_once(session, settings=learning_settings)
    return {
        "status": result.status,
        "paper_trades_created": result.paper_trades_created,
        "forecasts_generated": result.forecasts_generated,
        "opportunities_found": result.opportunities_found,
        "errors": len(result.errors),
        "min_score": str(learning_settings.learning_min_opportunity_score),
        "min_edge": str(learning_settings.learning_min_edge),
        "candidate_scan_limit": learning_settings.learning_candidate_scan_limit,
    }


def _paper_pnl_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = calculate_and_store_pnl(session)
    return {"positions_evaluated": summary.positions_evaluated, "total_pnl": str(summary.total_pnl)}


def _sync_settlements_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    count = sync_settlements(lookback_days=30, limit=100, max_pages=1, session=session)
    return {"settlements_synced": count}


def _model_confidence_step(session: Session, settings: Settings) -> dict[str, Any]:
    from kalshi_predictor.confidence.engine import run_model_confidence_engine
    from kalshi_predictor.confidence.reports import generate_model_confidence_report

    result = run_model_confidence_engine(session, settings=settings, days=30)
    path = generate_model_confidence_report(session, settings=settings, refresh=False)
    return {
        "scores_inserted": result.scores_inserted,
        "weights_inserted": result.weights_inserted,
        "path": str(path),
    }


def _learning_report_step(session: Session, settings: Settings) -> dict[str, Any]:
    path = generate_learning_report(session, settings=settings)
    return {"path": str(path)}


def _signals_report_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    path = generate_signal_report(session, output_path=Path("reports/signals_report.md"))
    return {"path": str(path)}


def _paper_summary_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    summary = get_paper_summary(session)
    return {"total_orders": summary.total_orders, "open_orders": summary.open_orders}


def _leaderboard_step(session: Session, settings: Settings) -> dict[str, Any]:
    del settings
    path, result = generate_leaderboard_report(
        session,
        days=30,
        output_path=Path("reports/model_leaderboard.md"),
    )
    return {"path": str(path), "models_compared": len(result.rows)}


def _overnight_report_step(session: Session, settings: Settings) -> dict[str, Any]:
    path = generate_overnight_report(session, settings=settings)
    return {"path": str(path)}
