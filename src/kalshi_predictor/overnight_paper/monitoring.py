"""Live owner-bound settlement evidence; no serialized ready flags authorize entry."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.overnight_paper.provenance import Verification
from kalshi_predictor.overnight_paper.runtime_owner import RuntimeOwner, validate_runtime_owner
from kalshi_predictor.overnight_paper.settlement import market_lifecycle
from kalshi_predictor.overnight_paper.settlement_runner import (
    SettlementRunReport,
    run_settlement_cycles,
)
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.overnight_paper.store import digest
from kalshi_predictor.overnight_paper.watcher import PublicMarketObservation


@dataclass(frozen=True, eq=False)
class MonitoringPermit:
    owner: RuntimeOwner
    shadow_id: str
    receipt_id: str
    database_id: str
    code_sha: str
    started_at: datetime
    completed_at: datetime
    monotonic_at: float


@dataclass(frozen=True)
class MonitoringCycle:
    report: SettlementRunReport
    permits: tuple[MonitoringPermit, ...]


_ACTIVE: dict[str, tuple[MonitoringPermit, ...]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def revoke_monitoring(owner: RuntimeOwner) -> None:
    """Any stop, entry disable or failed cycle discards prior admission evidence."""
    _ACTIVE.pop(owner.generation, None)


def _baseline_id(session: Session) -> str:
    rows = session.execute(text(
        "SELECT id,payload FROM overnight_sprint_cycles "
        "WHERE id LIKE 'authorization-baseline:%'"
    )).all()
    if len(rows) != 1:
        raise ValueError("MONITOR_SINGLE_AUTHORIZATION_BASELINE_REQUIRED")
    key, raw = rows[0]
    payload = json.loads(raw)
    database_id = payload["database_id"]
    if (payload.get("kind") != "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1"
            or key != "authorization-baseline:" + database_id):
        raise ValueError("MONITOR_AUTHORIZATION_BASELINE_INVALID")
    return str(database_id)


def _receipt(session: Session, receipt_id: str, shadow_id: str,
             now: datetime, earliest: datetime) -> PublicMarketObservation:
    raw = session.execute(text(
        "SELECT payload FROM overnight_sprint_cycles WHERE id=:key"
    ), {"key": receipt_id}).scalar_one_or_none()
    if raw is None:
        raise ValueError("MONITOR_RECEIPT_MISSING")
    receipt = json.loads(raw)
    if (receipt.get("kind") != "OVERNIGHT_WATCHER_OBSERVATION_V1"
            or receipt_id != "watcher-observation:" + digest(receipt)):
        raise ValueError("MONITOR_RECEIPT_INTEGRITY")
    source = receipt["source"]
    observation = PublicMarketObservation(
        source["ticker"], source["source_url"], aware(source["captured_at"]),
        source["sha256"], source["payload"].encode("utf-8"),
    )
    market = observation.market(now=now)
    if observation.captured_at < earliest:
        raise ValueError("MONITOR_PREVIOUS_CYCLE_RECEIPT")
    state = market_lifecycle(observation.ticker, market, now=now)
    if (state["state"] != "OPEN" or receipt["lifecycle"]["state"] != "OPEN"
            or market.get("status") not in {"active", "open"}):
        raise ValueError("MONITOR_CANDIDATE_NOT_OPEN")
    shadow_raw = session.execute(text(
        "SELECT payload FROM overnight_shadow WHERE id=:key"
    ), {"key": shadow_id}).scalar_one_or_none()
    if shadow_raw is None:
        raise ValueError("MONITOR_SHADOW_REQUIRED")
    shadow = json.loads(shadow_raw)
    if (digest(shadow) != shadow_id or shadow["ticker"] != observation.ticker
            or shadow["event_ticker"] != market.get("event_ticker")
            or shadow["series_ticker"] != market.get("series_ticker", shadow["series_ticker"])
            or aware(shadow["close_time"]) != aware(market["close_time"])):
        raise ValueError("MONITOR_SHADOW_IDENTITY_MISMATCH")
    return observation


def monitor_owned_once(*, owner: RuntimeOwner, session_factory: sessionmaker[Session],
                       code_sha: str) -> MonitoringCycle:
    """Run the actual public watcher before minting short-lived admission permits.

    No transport/callback or supplied report is accepted. The supervising caller
    keeps ownership and repeats this cycle even after entry has been disabled.
    """
    from kalshi_predictor.overnight_paper.coordinator import (
        _owned_session_factory,
        _verify_database,
    )

    path = owner.database_path
    validate_runtime_owner(owner, path)
    revoke_monitoring(owner)
    factory = _owned_session_factory(session_factory, path)
    started = _now()
    report = run_settlement_cycles(session_factory=factory, database_path=path, cycles=1)
    completed = _now()
    if not 0 <= (completed - started).total_seconds() <= 60:
        raise ValueError("MONITOR_CYCLE_CLOCK_OR_DURATION_INVALID")
    validate_runtime_owner(owner, path)
    permits: list[MonitoringPermit] = []
    with factory() as session:
        _verify_database(session, path)
        database_id = _baseline_id(session)
        for batch in report.reports:
            for row in batch.rows:
                if row.get("state") != "OPEN":
                    continue
                for receipt_id in batch.observation_ids:
                    # A batch may include several different tracked tickers.
                    raw = session.execute(text(
                        "SELECT payload FROM overnight_sprint_cycles WHERE id=:key"
                    ), {"key": receipt_id}).scalar_one()
                    if json.loads(raw)["source"]["ticker"] != row["ticker"]:
                        continue
                    _receipt(session, receipt_id, row["shadow_id"], completed, started)
                    permits.append(MonitoringPermit(
                        owner, row["shadow_id"], receipt_id, database_id, code_sha,
                        started, completed, time.monotonic(),
                    ))
    _ACTIVE[owner.generation] = tuple(permits)
    return MonitoringCycle(report, tuple(permits))


def verify_monitoring(session: Session, permit: MonitoringPermit | None, *,
                      database_path: Path, database_id: str, code_sha: str,
                      shadow_id: str) -> Verification:
    """Read actual live ownership and the same transaction's original receipt."""
    scope = "LIVE_SAME_LEDGER_SETTLEMENT_MONITOR"
    try:
        if type(permit) is not MonitoringPermit:
            raise ValueError("LIVE_MONITORING_PERMIT_REQUIRED")
        if not any(item is permit for item in _ACTIVE.get(permit.owner.generation, ())):
            raise ValueError("MONITOR_PERMIT_NOT_ACTIVE")
        validate_runtime_owner(permit.owner, database_path)
        if (permit.database_id != database_id or permit.code_sha != code_sha
                or permit.shadow_id != shadow_id):
            raise ValueError("MONITOR_RELEASE_IDENTITY_MISMATCH")
        if not session.in_transaction():
            raise ValueError("MONITOR_ADMISSION_TRANSACTION_REQUIRED")
        databases = session.execute(text("PRAGMA database_list")).all()
        main = [row for row in databases if row[1] == "main"]
        if (len(main) != 1 or Path(main[0][2]).resolve() != database_path.resolve()
                or any(row[1] not in {"main", "temp"} for row in databases)):
            raise ValueError("MONITOR_DATABASE_PATH_MISMATCH")
        if _baseline_id(session) != database_id:
            raise ValueError("MONITOR_DATABASE_ID_MISMATCH")
        now = _now()
        if (not 0 <= time.monotonic() - permit.monotonic_at <= 60
                or not 0 <= (now - permit.completed_at).total_seconds() <= 60):
            raise ValueError("MONITOR_PERMIT_EXPIRED_OR_CLOCK_INVALID")
        observation = _receipt(session, permit.receipt_id, shadow_id, now, permit.started_at)
        return Verification(True, (), (observation.sha256,), scope=scope)
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        return Verification(False, (str(exc),), scope=scope)
