"""Transactional adapter to the existing LOCAL simulator; never an exchange gateway.

Not invoked by discovery. The caller must supply current semantic validator artifacts,
full-suite/exact-SHA release evidence, and an identical already-persisted shadow.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.advanced_risk.engine import AdvancedRiskConfig, AdvancedRiskEngine
from kalshi_predictor.advanced_risk.service import advanced_risk_request_for_paper_decision
from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    AdvancedRiskDecisionLog,
    Forecast,
    Market,
    MarketSnapshot,
    PaperFill,
    PositionSizingDecisionLog,
)
from kalshi_predictor.overnight_paper.boundary import (
    LocalPaperAuthorization,
    authorization_fingerprint,
    validate_authorization,
)
from kalshi_predictor.overnight_paper.qualification import (
    EvidenceReference,
    Readiness,
    decision_fingerprint,
    qualify_candidate,
)
from kalshi_predictor.overnight_paper.store import aware, digest, encode
from kalshi_predictor.overnight_paper.watcher import verified_settled_tickers
from kalshi_predictor.paper.ledger import create_paper_order
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.paper.simulator import simulate_immediate_fill
from kalshi_predictor.position_sizing.service import size_paper_decision


def _running_repository() -> Path:
    return Path(__file__).resolve().parents[3]


def _verify_import_origins(repository: Path) -> None:
    if repository.resolve() != _running_repository():
        raise ValueError("RELEASE_REPOSITORY_NOT_RUNNING_CHECKOUT")
    source_root = repository.resolve() / "src"
    for name in (
        "kalshi_predictor.paper.ledger",
        "kalshi_predictor.paper.simulator",
        "kalshi_predictor.overnight_paper.qualification",
        "kalshi_predictor.config",
        "kalshi_predictor.advanced_risk.service",
        "kalshi_predictor.position_sizing.service",
    ):
        origin = getattr(sys.modules.get(name), "__file__", None)
        if origin is None or not Path(origin).resolve().is_relative_to(source_root):
            raise ValueError("MIXED_RUNTIME_IMPORT_CHECKOUT")


@dataclass(frozen=True)
class ExactReleaseEvidence:
    repository: Path
    sha: str
    report: EvidenceReference
    pytest_output: EvidenceReference
    lint_output: EvidenceReference
    hosted_checks: EvidenceReference
    mypy_output: EvidenceReference | None = None

    def verify(self) -> None:
        _verify_import_origins(self.repository)
        artifacts = (self.report, self.pytest_output, self.lint_output, self.hosted_checks)
        if not all(item.valid() for item in artifacts):
            raise ValueError("RELEASE_ARTIFACT_INTEGRITY")

        def git(*args: str) -> str:
            return subprocess.check_output(
                ["git", "-C", str(self.repository), *args],
                text=True,
                timeout=10,
            ).strip()

        if git("rev-parse", "HEAD") != self.sha or git("status", "--porcelain"):
            raise ValueError("EXACT_SHA_CLEAN_RELEASE_REQUIRED")
        report = json.loads(self.report.payload)
        checks = json.loads(self.hosted_checks.payload)
        if not isinstance(report, dict) or report.get("sha") != self.sha:
            raise ValueError("RELEASE_SHA_MISMATCH")
        expected = {
            "pytest": self.pytest_output.sha256,
            "ruff": self.lint_output.sha256,
            "hosted_checks": self.hosted_checks.sha256,
        }
        if self.mypy_output is not None:
            expected["mypy"] = self.mypy_output.sha256
        if report.get("artifacts") != expected or report.get("pytest_command") != "pytest":
            raise ValueError("FULL_SUITE_EVIDENCE_REQUIRED")
        if report.get("lint_command") != "ruff check .":
            raise ValueError("FULL_LINT_EVIDENCE_REQUIRED")
        output = self.pytest_output.payload.decode()
        if not re.search(r"\b[1-9][0-9]* passed\b", output) or re.search(
            r"\b[1-9][0-9]* (failed|errors?)\b", output
        ):
            raise ValueError("FULL_SUITE_NOT_GREEN")
        if "All checks passed!" not in self.lint_output.payload.decode():
            raise ValueError("LINT_NOT_GREEN")
        if not isinstance(checks, dict) or not checks.get("check_runs"):
            raise ValueError("HOSTED_CHECKS_MISSING")
        for check in checks["check_runs"]:
            if check.get("head_sha") != self.sha or check.get("conclusion") != "success":
                raise ValueError("HOSTED_CHECKS_NOT_GREEN_AT_SHA")
        names = {check.get("name") for check in checks["check_runs"]}
        required = report.get("required_checks")
        if not isinstance(required, list) or not required or not set(required) <= names:
            raise ValueError("REQUIRED_CHECKS_UNPROVEN")
        config = self.repository / "pyproject.toml"
        mypy_configured = (config.is_file() and "[tool.mypy]" in config.read_text()) or any(
            (self.repository / name).exists() for name in ("mypy.ini", ".mypy.ini")
        )
        if mypy_configured and (
            self.mypy_output is None
            or not self.mypy_output.valid()
            or "Success: no issues found" not in self.mypy_output.payload.decode()
            or report.get("mypy_command") != "mypy src"
        ):
            raise ValueError("MYPY_EVIDENCE_REQUIRED")


@dataclass(frozen=True)
class ActivationResult:
    shadow_id: str
    paper_order_id: int
    fill_created: bool
    actual_simulated_fee: Decimal | None


def _utc(value: datetime) -> datetime:
    # SQLite stores UTC DateTime columns without tzinfo.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _flags(settings: Settings) -> None:
    if any(
        (
            settings.execution_enabled,
            settings.autopilot_enabled,
            not settings.execution_dry_run,
            not settings.autopilot_dry_run,
            not settings.execution_kill_switch,
            settings.execution_gateway_mode != "disabled",
        )
    ):
        raise ValueError("EXCHANGE_OR_AUTOPILOT_PATH_ENABLED")
    if not settings.paper_order_creation_enabled or settings.paper_order_kill_switch:
        raise ValueError("LOCAL_PAPER_FLAGS_NOT_AUTHORIZED")
    if settings.learning_mode:
        raise ValueError("LEARNING_OVERRIDE_NOT_ALLOWED")


def _validate_engine_records(
    session: Session,
    decision: PaperDecision,
    args: dict[str, Any],
    expected_order_id: int | None = None,
) -> None:
    for key, cls, expected in (
        ("position_sizing_decision_id", PositionSizingDecisionLog, args["phase3m"]),
        ("advanced_risk_decision_id", AdvancedRiskDecisionLog, args["phase3n"]),
    ):
        record_id = decision.raw_decision_json.get(key)
        if type(record_id) is not int:
            raise ValueError("PERSISTED_ENGINE_RECORD_REQUIRED")
        record = session.get(cls, record_id)
        if (
            not isinstance(record, PositionSizingDecisionLog | AdvancedRiskDecisionLog)
            or record.ticker != decision.ticker
            or record.paper_order_id != expected_order_id
        ):
            raise ValueError("ENGINE_RECORD_IDENTITY_OR_REUSE")
        payload = json.loads(record.raw_json)
        subset = {key: payload.get(key) for key in expected.as_dict()}
        if decision_fingerprint(subset) != decision_fingerprint(expected.as_dict()):
            raise ValueError("ENGINE_OUTPUT_CHANGED_AFTER_SHADOW")
        if record.executed_contracts != 1 or expected.executed_contracts != 1:
            raise ValueError("ENGINE_QUANTITY_NOT_ONE")


def _validate_shadow_inputs(
    session: Session,
    decision: PaperDecision,
    shadow: dict[str, Any],
    args: dict[str, Any],
    now: datetime,
    hard_horizon_hours: int = 72,
) -> None:
    inputs = args["decision_inputs"]
    if any(
        _utc(args[key].decision_timestamp) != aware(shadow["decision_at"])
        for key in ("phase3m", "phase3n")
    ):
        raise ValueError("ENGINE_DECISION_CLOCK_MISMATCH")
    if shadow.get("qualification_inputs") != inputs:
        raise ValueError("SHADOW_QUALIFICATION_INPUT_MISMATCH")
    pairs = {
        "ticker": decision.ticker,
        "model": decision.model_name,
        "side": decision.side,
        "event_ticker": inputs.get("event_id"),
        "series_ticker": inputs.get("series"),
        "model_version": inputs.get("model_version"),
        "settlement_rule_version": inputs.get("rule_version"),
    }
    if any(shadow.get(key) != value for key, value in pairs.items()):
        raise ValueError("SHADOW_DECISION_IDENTITY_MISMATCH")
    if (
        inputs.get("side") != decision.side
        or Decimal(str(inputs.get("executable_price"))) != decision.limit_price
    ):
        raise ValueError("QUALIFIED_SIDE_OR_EXECUTABLE_PRICE_MISMATCH")
    books = []
    for evidence in args.get("evidence", ()):
        if evidence.gate == 5:
            for source in evidence.sources:
                envelope = json.loads(source.payload)
                if envelope.get("url") == (
                    "https://external-api.kalshi.com/trade-api/v2/markets/"
                    + decision.ticker
                    + "/orderbook"
                ):
                    books.append(envelope["body"])
    if len(books) != 1 or books[0] != shadow.get("snapshot"):
        raise ValueError("QUALIFIED_PUBLIC_BOOK_SHADOW_MISMATCH")
    if decision.quantity != 1 or decision.side not in {BUY_YES, BUY_NO}:
        raise ValueError("ONE_CONTRACT_BUY_ONLY")
    if (
        Decimal(str(shadow.get("forecast"))) != decision.probability
        or Decimal(str(shadow.get("price"))) != decision.limit_price
    ):
        raise ValueError("SHADOW_DECISION_PRICE_OR_FORECAST_MISMATCH")
    ev = args["ev"]
    if (
        Decimal(str(shadow.get("net_ev"))) != ev.net_ev
        or ev.executable_price != decision.limit_price
        or ev.model_probability
        != (decision.probability if decision.side == BUY_YES else 1 - decision.probability)
    ):
        raise ValueError("SHADOW_EXECUTABLE_EV_MISMATCH")
    if (
        shadow.get("sizing") != args["phase3m"].as_dict()
        or shadow.get("risk") != args["phase3n"].as_dict()
    ):
        raise ValueError("SHADOW_ENGINE_OUTPUT_MISMATCH")
    if not 0 <= (now - aware(shadow["decision_at"])).total_seconds() <= 60:
        raise ValueError("SHADOW_REQUALIFICATION_REQUIRED")
    market = session.get(Market, decision.ticker)
    forecast = session.get(Forecast, decision.forecast_id)
    snapshot = session.get(MarketSnapshot, inputs.get("snapshot_id"))
    if market is None or forecast is None or snapshot is None:
        raise ValueError("APPLICATION_PROVENANCE_ROWS_REQUIRED")
    if (
        market.event_ticker != inputs.get("event_id")
        or market.series_ticker != inputs.get("series")
        or forecast.ticker != decision.ticker
        or snapshot.ticker != decision.ticker
        or str(inputs.get("forecast_id")) != str(forecast.id)
        or forecast.model_name != decision.model_name
        or Decimal(forecast.yes_probability) != decision.probability
    ):
        raise ValueError("APPLICATION_INPUT_IDENTITY_MISMATCH")
    latest = session.scalar(
        select(MarketSnapshot.id)
        .where(MarketSnapshot.ticker == decision.ticker)
        .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
        .limit(1)
    )
    if latest != snapshot.id:
        raise ValueError("NEW_SNAPSHOT_REQUIRES_NEW_SHADOW")
    if (
        aware(shadow["forecast_at"]) != _utc(forecast.forecasted_at)
        or aware(shadow["snapshot_at"]) != _utc(snapshot.captured_at)
        or market.close_time is None
        or aware(shadow["close_time"]) != _utc(market.close_time)
    ):
        raise ValueError("APPLICATION_CLOCK_MISMATCH")
    if json.loads(snapshot.raw_orderbook_json or "null") != shadow.get("snapshot"):
        raise ValueError("APPLICATION_BOOK_MISMATCH")
    if market.expiration_time is None:
        raise ValueError("VERIFIED_SETTLEMENT_LONGSTOP_REQUIRED")
    longstop = _utc(market.expiration_time)
    if not now < longstop <= now + timedelta(hours=min(hard_horizon_hours, 72)):
        raise ValueError("HARD_SETTLEMENT_LONGSTOP_EXCEEDED")
    if (
        aware(shadow["latest_settlement_at"]) != longstop
        or aware(inputs["latest_settlement_at"]) != longstop
        or aware(shadow["expected_settlement_at"]) > longstop
    ):
        raise ValueError("SHADOW_SETTLEMENT_LONGSTOP_MISMATCH")
    if market.status not in {"open", "active"}:
        raise ValueError("MARKET_NOT_OPEN")


def _revalidate_engines(
    session: Session,
    decision: PaperDecision,
    args: dict[str, Any],
    settings: Settings,
    now: datetime,
) -> None:
    # Re-read the actual local portfolio and frozen inputs with unchanged config.
    # A changed result requires a new shadow; no favorable replacement is accepted.
    sized = size_paper_decision(
        session, decision=decision, settings=settings, decision_timestamp=now
    ).decision
    request = advanced_risk_request_for_paper_decision(
        session,
        decision=decision,
        settings=settings,
        phase_3m_decision=sized,
        decision_timestamp=now,
    )
    risk = AdvancedRiskEngine(AdvancedRiskConfig.from_settings(settings)).decide(request)
    if decision_fingerprint(sized.as_dict()) != decision_fingerprint(
        args["phase3m"].as_dict()
    ) or decision_fingerprint(risk.as_dict()) != decision_fingerprint(args["phase3n"].as_dict()):
        raise ValueError("ENGINE_REVALIDATION_REQUIRES_NEW_SHADOW")


def _open_position_count(session: Session, *, now: datetime) -> int:
    verified_final = verified_settled_tickers(session, now=now)
    open_tickers = session.execute(
        text("SELECT ticker FROM paper_positions WHERE yes_contracts<>0 OR no_contracts<>0")
    ).scalars()
    return sum(ticker not in verified_final for ticker in open_tickers)


def activate_local_paper(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    authorization: LocalPaperAuthorization,
    objective_bytes: bytes,
    release: ExactReleaseEvidence,
    qualification_args: dict[str, Any],
    shadow_payload: dict[str, Any],
    shadow_id: str,
    decision: PaperDecision,
    settings: Settings,
    now: datetime,
) -> ActivationResult:
    """Fail/rollback atomically. Caller never supplies a transport or gateway.

    Passing this function requires runtime flags to be explicitly changed for LOCAL
    paper only after release/readiness approval; this adapter never changes flags.
    """
    release.verify()
    _flags(settings)
    if "position_sizing_historical_evidence_cache" in decision.raw_decision_json:
        raise ValueError("UNVERIFIED_HISTORY_CACHE_REQUIRES_RECOMPUTATION")
    if hashlib.sha256(objective_bytes).hexdigest() != authorization.objective_sha256:
        raise ValueError("OPERATOR_OBJECTIVE_MISMATCH")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in (database_path, *database_path.parents)
    ):
        raise ValueError("ISOLATED_UNLINKED_DATABASE_REQUIRED")
    if not authorization.isolated_database_path or not authorization.database_id:
        raise ValueError("AUTHORIZATION_DATABASE_BINDING_REQUIRED")
    if Path(authorization.isolated_database_path).resolve() != path:
        raise ValueError("AUTHORIZATION_DATABASE_PATH_MISMATCH")
    expected_authorization = authorization_fingerprint(authorization)
    args = dict(qualification_args)
    if args.get("decision_inputs", {}).get("authorization_sha256") != expected_authorization:
        raise ValueError("QUALIFIED_AUTHORIZATION_BINDING_MISMATCH")
    if args.get("decision_inputs", {}).get("code_sha") != release.sha:
        raise ValueError("QUALIFIED_CODE_SHA_MISMATCH")
    for item in args.get("evidence", ()):
        if not item.verified(args["decision_inputs"], as_of=now):
            raise ValueError("SEMANTIC_GATE_REVALIDATION_FAILED")
        artifact = json.loads(item.reference.payload)
        if not aware(artifact["validated_at"]) <= now < aware(artifact["valid_until"]):
            raise ValueError("GATE_EVIDENCE_EXPIRED")
    if args.get("minimum_net_ev") != settings.paper_min_edge:
        raise ValueError("UNCHANGED_EV_THRESHOLD_REQUIRED")
    if args.get("decision_inputs", {}).get("config_hash") != decision_fingerprint(
        settings.model_dump(mode="json")
    ):
        raise ValueError("QUALIFIED_SETTINGS_HASH_MISMATCH")
    if (
        args.get("ev") is not None
        and args["ev"].estimated_fee < settings.paper_default_fee_per_contract
    ):
        raise ValueError("SIMULATOR_FEE_EXCEEDS_EV_ALLOWANCE")
    if qualify_candidate(**args).status != Readiness.PAPER_ELIGIBLE:
        raise ValueError("ALL_TWELVE_GATES_REQUIRED")
    with session_factory() as session:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            actual = session.execute(text("PRAGMA database_list")).all()
            main = [row for row in actual if row[1] == "main"]
            foreign = [row for row in actual if row[1] not in {"main", "temp"}]
            if len(main) != 1 or foreign or Path(main[0][2]).resolve() != path:
                raise ValueError("DATABASE_PATH_MISMATCH")
            if session.execute(text("PRAGMA integrity_check")).scalar() != "ok":
                raise ValueError("DATABASE_INTEGRITY_FAILED")
            marker = session.execute(
                text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"),
                {"id": "authorization-baseline:" + authorization.database_id},
            ).scalar_one_or_none()
            expected_marker = {
                "kind": "LOCAL_PAPER_AUTHORIZATION_BASELINE_V1",
                "database_id": authorization.database_id,
                "database_path": str(path),
                "objective_sha256": authorization.objective_sha256,
                "authorization_sha256": expected_authorization,
                "baseline_paper_orders": 0,
                "baseline_paper_fills": 0,
            }
            if marker is None or json.loads(marker) != expected_marker:
                raise ValueError("IMMUTABLE_AUTHORIZATION_BASELINE_REQUIRED")
            row = session.execute(
                text("SELECT payload,paper_order_id FROM overnight_shadow WHERE id=:key"),
                {"key": shadow_id},
            ).first()
            if (
                row is None
                or row[0] != encode(shadow_payload)
                or digest(shadow_payload) != shadow_id
            ):
                raise ValueError("SHADOW_PAPER_SNAPSHOT_MISMATCH")
            if row[1] is not None:
                raise ValueError("SHADOW_ALREADY_ACTIVATED")
            total = session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one()
            linked = session.execute(
                text("SELECT count(*) FROM overnight_shadow WHERE paper_order_id IS NOT NULL")
            ).scalar_one()
            if total != linked:
                raise ValueError("UNRECONCILED_PAPER_ORDERS")
            events = frozenset(
                session.execute(
                    text(
                        "SELECT event_ticker FROM overnight_shadow WHERE paper_order_id IS NOT NULL"
                    )
                ).scalars()
            )
            open_count = _open_position_count(session, now=now)
            errors = validate_authorization(
                authorization,
                now=now,
                contracts=decision.quantity,
                new_positions=total,
                open_positions=open_count,
                event_id=shadow_payload["event_ticker"],
                used_events=events,
                settlement_at=aware(shadow_payload["expected_settlement_at"]),
            )
            if errors:
                raise ValueError(",".join(errors))
            _validate_shadow_inputs(
                session, decision, shadow_payload, args, now, authorization.hard_horizon_hours
            )
            _revalidate_engines(
                session, decision, args, settings, aware(shadow_payload["decision_at"])
            )
            _validate_engine_records(session, decision, args)
            order = create_paper_order(session, decision, settings=settings)
            if order is None or order.quantity != 1:
                raise ValueError("LOCAL_LEDGER_REJECTED_OR_CHANGED_QUANTITY")
            if (
                order.ticker != decision.ticker
                or order.side != decision.side
                or Decimal(order.limit_price) != decision.limit_price
            ):
                raise ValueError("LOCAL_LEDGER_CHANGED_DECISION")
            _validate_engine_records(session, decision, args, expected_order_id=order.id)
            fill = simulate_immediate_fill(session, order, settings=settings)
            if fill is not None and not isinstance(fill, PaperFill):
                raise ValueError("LOCAL_SIMULATOR_FILL_TYPE_MISMATCH")
            if fill is not None and (
                fill.quantity != 1 or Decimal(fill.price) != decision.limit_price
            ):
                raise ValueError("LOCAL_SIMULATOR_CHANGED_FILL")
            session.execute(
                text(
                    "UPDATE overnight_shadow SET paper_order_id=:order_id "
                    "WHERE id=:key AND paper_order_id IS NULL"
                ),
                {"key": shadow_id, "order_id": order.id},
            )
            if session.execute(text("SELECT changes()")).scalar_one() != 1:
                raise ValueError("IDEMPOTENCY_LINK_FAILED")
            result = ActivationResult(
                shadow_id, order.id, fill is not None, None if fill is None else Decimal(fill.fee)
            )
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
