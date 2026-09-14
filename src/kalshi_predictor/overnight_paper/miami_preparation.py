"""Dedicated Miami preparation in isolated SQLite; never paper admission.

The default API accepts memory sessions only. A dedicated owned-file entry uses
the same genuine engine path and caller transaction; no NOAA rows, orders, fills,
reservations, or commits occur inside preparation.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kalshi_predictor.advanced_risk.engine import (
    AdvancedRiskConfig,
    AdvancedRiskDecision,
    AdvancedRiskEngine,
    AdvancedRiskRequest,
)
from kalshi_predictor.advanced_risk.repository import insert_advanced_risk_decision
from kalshi_predictor.advanced_risk.service import advanced_risk_request_for_paper_decision
from kalshi_predictor.config import Settings
from kalshi_predictor.data.repositories import encode_json, insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import Base, MarketSnapshot
from kalshi_predictor.forecasting.base import ForecastOutput
from kalshi_predictor.opportunities.scoring import score_liquidity
from kalshi_predictor.paper.fees import CONTRACT_KEY, build_fee_quote, decision_fee_quote
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, PaperDecision
from kalshi_predictor.position_sizing.service import size_paper_decision
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision
from kalshi_predictor.utils.time import utc_now

from .books import qualify_book
from .coordinator import assert_public_only_settings
from .miami_binding import MiamiOriginal, _at, _decode
from .miami_provenance import (
    miami_feature_record,
    miami_source_bundle,
    verify_miami_provenance_source,
)
from .miami_source import _receipt
from .miami_source_gate import MiamiGateContext
from .miami_storage import MiamiOwnedStorage, verify_miami_storage
from .provenance import Artifact, canonical_hash
from .qualification import compute_net_ev


@dataclass(frozen=True)
class MiamiEngineOutputs:
    decision: PaperDecision
    phase3m: PositionSizingDecision
    phase3n: AdvancedRiskDecision
    risk_request: AdvancedRiskRequest
    forecast_output: ForecastOutput


@dataclass(frozen=True)
class MiamiPreparationResult:
    """Separate type: cannot be passed to the NOAA candidate assembler."""

    state: str
    ticker: str
    blockers: tuple[str, ...]
    records: dict[str, Any]
    source_bundle: dict[str, Any] | None = None
    original_context: MiamiGateContext | None = None
    orderbook: MiamiOriginal | None = None
    orderbook_receipt: Artifact | None = None
    engine_outputs: MiamiEngineOutputs | None = None
    paper_eligible: bool = False
    execution_authority: bool = False
    owned_storage: MiamiOwnedStorage | None = None


@dataclass(frozen=True)
class MiamiDevelopmentPreparationResult(MiamiPreparationResult):
    """Visible-price research only; never an admission preparation result."""


def _development_book_structure(book: dict[str, Any]) -> dict[str, Any]:
    """Preserve admission results; check actual visible one-contract quotes separately."""
    if book["tick_status"] != "VERIFIED" or not 0 <= book["receipt_age_seconds"] <= 60:
        raise ValueError("DEVELOPMENT_FRESH_VALID_TICK_BOOK_REQUIRED")
    sides = {}
    for key in ("YES", "NO"):
        row = book["sides"][key]
        bid, ask, bid_depth, ask_depth = (
            Decimal(str(row[field])) for field in ("bid", "ask", "bid_depth", "ask_depth")
        )
        if (
            any(not value.is_finite() for value in (bid, ask, bid_depth, ask_depth))
            or not 0 < bid <= ask < 1
            or bid_depth < 1
            or ask_depth < 1
        ):
            raise ValueError("DEVELOPMENT_VISIBLE_ONE_CONTRACT_BOOK_REQUIRED")
        sides[key] = dict(
            bid=str(bid),
            ask=str(ask),
            bid_depth=str(bid_depth),
            ask_depth=str(ask_depth),
            buy_price_source=row["buy_price_source"],
            visible_one_contract=True,
        )
    return dict(
        scope="VISIBLE_ONE_CONTRACT_RESEARCH_NOT_ADMISSION_QUALIFICATION",
        admission_qualification_sha256=canonical_hash(book),
        sides=sides,
    )


def _snapshot_id(session: Session, ticker: str) -> int | None:
    row = session.scalar(
        select(MarketSnapshot)
        .where(MarketSnapshot.ticker == ticker)
        .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
        .limit(1)
    )
    return None if row is None else row.id


def _orm_at(value: datetime) -> datetime:
    """SQLite driver alone may return naive UTC; external receipts remain strict."""
    return _at(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


def _isolated_connection(session: Session, storage: MiamiOwnedStorage | None = None):
    if storage is not None:
        return verify_miami_storage(session, storage, now=utc_now())
    if type(session) is not Session or session.get_bind().dialect.name != "sqlite":
        raise ValueError("ISOLATED_SQLITE_SESSION_REQUIRED")
    if session.new or session.dirty or session.deleted:
        raise ValueError("UNFLUSHED_CALLER_CHANGES_FORBIDDEN")
    engine = session.get_bind()
    if not isinstance(engine, Engine) or engine.url.database not in (
        None,
        "",
        ":memory:",
    ):
        raise ValueError("IN_MEMORY_SQLITE_ONLY")
    if any(
        session.get_bind(mapper=mapper.class_) is not engine for mapper in Base.registry.mappers
    ):
        raise ValueError("SPLIT_SESSION_BINDS_FORBIDDEN")
    connection = session.connection()
    if any(
        row[1] not in {"main", "temp"} or row[2]
        for row in connection.exec_driver_sql("PRAGMA database_list")
    ):
        raise ValueError("ATTACHED_DATABASE_FORBIDDEN")
    return connection


def assert_miami_settings(settings: Settings) -> None:
    if type(settings) is not Settings:
        raise ValueError("CONCRETE_SETTINGS_REQUIRED")
    assert_public_only_settings(settings)
    try:
        configured_url = urlsplit(settings.kalshi_db_url)
    except ValueError:
        raise ValueError("INVALID_CONFIGURED_DATABASE_URL") from None
    if (
        configured_url.username is not None
        or configured_url.password is not None
        or configured_url.query
        or configured_url.fragment
    ):
        raise ValueError("CREDENTIAL_OR_QUERY_BEARING_DATABASE_URL_FORBIDDEN")
    if (
        settings.execution_enabled
        or not settings.execution_dry_run
        or not settings.execution_kill_switch
        or settings.execution_gateway_mode != "disabled"
        or settings.autopilot_enabled
    ):
        raise ValueError("LOCAL_ONLY_SETTINGS_REQUIRED")


def _prepare_miami_candidate(
    session: Session,
    *,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None,
    _storage: MiamiOwnedStorage | None = None,
    _development: bool = False,
) -> MiamiPreparationResult:
    """Run actual forecast persistence, sizing and risk after exact replay/book/fees.

    The public default is memory-only; dedicated storage requires an active
    owner and exact authorization baseline for an isolated file.
    Successful computation remains unqualified without independent model/rule/
    final-settlement authority; a close/expiry timestamp is not a 72h certificate.
    """
    records: dict[str, Any] = {}
    ticker = ""
    result_type = MiamiDevelopmentPreparationResult if _development else MiamiPreparationResult
    try:
        if _development and _storage is None:
            raise ValueError("OWNED_DEVELOPMENT_STORAGE_REQUIRED")
        connection = _isolated_connection(session, _storage)
        assert_miami_settings(settings)
        if any(
            type(v) is not Decimal or not v.is_finite() or v < 0
            for v in (slippage_allowance, uncertainty_buffer)
        ):
            raise ValueError("FINITE_DECIMAL_COST_ALLOWANCES_REQUIRED")
        if slippage_allowance < settings.advanced_risk_estimated_slippage_per_contract:
            raise ValueError("SLIPPAGE_BELOW_RISK_CONFIGURATION")
        if uncertainty_buffer < settings.advanced_risk_gap_tail_buffer_per_contract:
            raise ValueError("UNCERTAINTY_BELOW_RISK_CONFIGURATION")
        started = utc_now()
        source = miami_source_bundle(context, decision_at=started)
        verified = verify_miami_provenance_source(source, decision_at=started, now=started)
        inputs = verified["inputs"]
        ticker = inputs["ticker"]
        if inputs["model_name"] != "miami_prior_day_increment_grid30_v1":
            raise ValueError("EXACT_MIAMI_GRID30_MODEL_REQUIRED")
        market = _decode(context.market.artifact)["market"]
        _receipt(orderbook, orderbook_receipt, context.market.url + "/orderbook")
        received = _at(orderbook.received_at)
        if not 0 <= (started - received).total_seconds() <= 60:
            raise ValueError("STALE_OR_FUTURE_ORDERBOOK")
        payload = _decode(orderbook.artifact)
        book = qualify_book(
            payload,
            received_at=received,
            now=started,
            max_spread=settings.opportunity_max_spread,
            liquidity_score=score_liquidity(
                volume=market.get("volume_fp"),
                open_interest=market.get("open_interest_fp"),
                liquidity=market.get("liquidity_dollars"),
            ),
            price_ranges=market.get("price_ranges"),
        )
        structure = _development_book_structure(book) if _development else None
        if not _development and not book["executable"]:
            raise ValueError("NO_EXECUTABLE_BOOK")
        if fee_evidence is None:
            raise ValueError("ORIGINAL_CERTIFIED_FEE_EVIDENCE_REQUIRED")
        # Fee captures must be these exact catalog bodies/receipts, not another
        # same-ticker metadata vintage that happens to yield the same fee.
        expected = {o.url: o for o in (context.market, context.event, context.series)}
        matched = set()
        for capture in fee_evidence["captures"]:
            row = _decode(Artifact(capture["sha256"], bytes.fromhex(capture["payload_hex"])))
            if row["url"] in expected:
                original = expected[row["url"]]
                if (
                    canonical_hash(row["body"]) != canonical_hash(_decode(original.artifact))
                    or _at(row["received_at"]) != _at(original.received_at)
                    or row["url"] in matched
                ):
                    raise ValueError("MIAMI_FEE_CATALOG_BINDING")
                matched.add(row["url"])
        if matched != set(expected):
            raise ValueError("MIAMI_FEE_CATALOG_BINDING")
        probability = Decimal(inputs["forecast_probability"])
        alternatives = []
        for side, key, p in ((BUY_YES, "YES", probability), (BUY_NO, "NO", 1 - probability)):
            if side == BUY_NO and not settings.paper_allow_buy_no:
                continue
            if (structure is not None and structure["sides"][key]["visible_one_contract"]) or (
                structure is None and book["sides"][key]["executable"]
            ):
                quote = build_fee_quote(
                    evidence=fee_evidence,
                    ticker=ticker,
                    side=side,
                    price=Decimal(book["sides"][key]["ask"]),
                    simulator_floor=settings.paper_default_fee_per_contract,
                    now=started,
                )
                if (
                    quote.decode()["event_id"] != inputs["event_id"]
                    or quote.decode()["series"] != inputs["series"]
                ):
                    raise ValueError("MIAMI_FEE_IDENTITY")
                ev = compute_net_ev(
                    model_probability=p,
                    executable_price=Decimal(book["sides"][key]["ask"]),
                    estimated_fee=quote.charge,
                    slippage_allowance=slippage_allowance,
                    uncertainty_buffer=uncertainty_buffer,
                )
                alternatives.append((ev.net_ev, side, ev, quote))
        if not alternatives:
            raise ValueError("NO_ALLOWED_EXECUTABLE_SIDE")
        _, side, ev, quote = max(alternatives, key=lambda item: item[0])
        # Savepoint rolls back this computation on expiry/identity failure, while
        # leaving caller-owned records untouched. It does not commit the session.
        # SQLite legacy mode can RELEASE a first savepoint as a database commit.
        # Establish an actual outer transaction before our rollback boundary.
        driver = connection.connection.driver_connection
        if not isinstance(driver, sqlite3.Connection):
            raise ValueError("CONCRETE_SQLITE_DRIVER_REQUIRED")
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
        with session.begin_nested():
            previous = session.scalar(
                select(MarketSnapshot)
                .where(MarketSnapshot.ticker == ticker)
                .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
                .limit(1)
            )
            if previous is not None and _orm_at(previous.captured_at) > received:
                raise ValueError("SNAPSHOT_SELECTION_MISMATCH")
            snapshot = insert_market_snapshot(
                session, dict(market, series_ticker=inputs["series"]), payload, received
            )
            feature_at = utc_now()
            feature = miami_feature_record(source, decision_at=feature_at, now=feature_at)
            midpoint = (Decimal(book["yes_bid"]) + Decimal(book["yes_ask"])) / 2
            generated = utc_now()
            output = ForecastOutput(
                ticker=ticker,
                forecasted_at=generated,
                model_name=inputs["model_name"],
                yes_probability=probability,
                market_mid_probability=midpoint,
                best_yes_bid=Decimal(book["yes_bid"]),
                best_yes_ask=Decimal(book["yes_ask"]),
                feature_json=dict(
                    miami_source_bundle_sha256=canonical_hash(source),
                    frozen_prediction_sha256=inputs["frozen_prediction_sha256"],
                    original_empirical_feature=feature,
                    model_version="1",
                    source_kind=inputs["source_kind"],
                    calibrated=False,
                ),
                notes="Original Miami grid30 replay; UNQUALIFIED; no NOAA substitution",
            )
            forecast = insert_forecast(
                session, output, market_snapshot_id=snapshot.id, attribution_enabled=True
            )
            available = utc_now()
            decision = PaperDecision(
                ticker,
                forecast.id,
                output.model_name,
                side,
                probability,
                ev.executable_price,
                ev.executable_price,
                ev.gross_edge,
                1,
                "Computed Miami diagnostic; independent release gates required",
                {CONTRACT_KEY: quote.decode()},
            )
            at = utc_now()
            sizing = size_paper_decision(
                session, decision=decision, settings=settings, decision_timestamp=at
            )
            request = advanced_risk_request_for_paper_decision(
                session,
                decision=decision,
                settings=settings,
                phase_3m_decision=sizing.decision,
                decision_timestamp=at,
            )
            risk = AdvancedRiskEngine(AdvancedRiskConfig.from_settings(settings)).decide(request)
            risk_row = insert_advanced_risk_decision(
                session,
                risk,
                request,
                ticker=ticker,
                position_sizing_decision_id=sizing.record_id,
                raw={
                    "guarded_fee_quote_sha256": quote.sha256,
                    "estimated_round_trip_fees": str(request.estimated_round_trip_fees),
                    "miami_preparation": {
                        "decision": asdict(decision),
                        "risk_request": asdict(request),
                        "settings": settings.model_dump(mode="json"),
                    },
                },
            )
            finished = utc_now()
            if any(
                type(value) is not int or value < 1
                for value in (forecast.id, snapshot.id, sizing.record_id, risk_row.id)
            ):
                raise ValueError("MIAMI_PERSISTED_ENGINE_IDS_REQUIRED")
            verify_miami_provenance_source(source, decision_at=at, now=finished)
            if (
                not _at(inputs["observation_time"]) > finished
                or not 0 <= (finished - received).total_seconds() <= 60
                or _snapshot_id(session, ticker) != snapshot.id
                or _orm_at(request.market_snapshot.captured_at) != received
                or request.estimated_round_trip_fees != quote.charge
            ):
                raise ValueError("MIAMI_FINAL_CLOCK_SNAPSHOT_OR_FEE_MISMATCH")
            records.update(
                forecast_id=forecast.id,
                preparation_started_at=started.isoformat(),
                snapshot_id=snapshot.id,
                sizing_id=sizing.record_id,
                risk_id=risk_row.id,
                forecast_generated_at=generated.isoformat(),
                forecast_available_at=available.isoformat(),
                decision_at=at.isoformat(),
                computation_finished_at=finished.isoformat(),
                forecast=asdict(output),
                book=payload,
                book_qualification=book,
                ev=asdict(ev),
                sizing=sizing.decision.as_dict(),
                sizing_evidence=sizing.evidence,
                risk=risk.as_dict(),
                risk_request=asdict(request),
                fee_contract=quote.decode(),
                model_input_as_of=inputs["model_input_as_of"],
                origin_at=inputs["origin_at"],
                observation_time=inputs["observation_time"],
                source_bundle_sha256=canonical_hash(source),
                paper_orders_created=0,
                model_kind="fixed_heuristic",
                settings=settings.model_dump(mode="json"),
            )
            if _storage is not None:
                records["owned_storage"] = dict(
                    database_path=str(_storage.database_path),
                    database_id=_storage.authorization.database_id,
                    generation=_storage.owner.generation,
                )
            if structure is not None:
                records["development_book_structure"] = structure
            risk_payload = json.loads(risk_row.raw_json)
            risk_payload["raw"]["miami_records_sha256"] = canonical_hash(
                json.loads(encode_json(records))
            )
            risk_row.raw_json = encode_json(risk_payload)
            session.flush()
            blockers = [
                "MODEL_RELEASE_AND_EVALUATION_REQUIRED",
                "CERTIFIED_RULE_AND_72H_FINALITY_REQUIRED",
                "MIAMI_CANDIDATE_ASSEMBLY_REQUIRED",
            ]
            if _development:
                blockers.append("DEVELOPMENT_ONLY_NO_ADMISSION_AUTHORITY")
                blockers.extend(
                    sorted(
                        {
                            row["first_blocker"]
                            for row in book["sides"].values()
                            if row["first_blocker"]
                        }
                    )
                )
            if ev.net_ev <= settings.paper_min_edge:
                blockers.append("POSITIVE_NET_EV")
            if sizing.decision.live_candidate_contracts < 1:
                blockers.append("PHASE_3M_NONZERO")
            if risk.action.value != "ALLOW" or risk.hard_blocks:
                blockers.append("PHASE_3N_ALLOW")
            engines = MiamiEngineOutputs(decision, sizing.decision, risk, request, output)
            if _storage is not None:
                verify_miami_storage(session, _storage, now=utc_now())
        return result_type(
            "DEVELOPMENT_COMPUTED_UNQUALIFIED" if _development else "COMPUTED_UNQUALIFIED",
            ticker,
            tuple(blockers),
            records,
            source,
            context,
            orderbook,
            orderbook_receipt,
            engines,
            owned_storage=_storage,
        )
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
        return result_type("BLOCKED", ticker, (str(exc),), {})


def _verify_miami_preparation_handoff(
    session: Session, result: MiamiPreparationResult, *, now: datetime, _development: bool = False
) -> None:
    """Verify actual records before a future assembler consumes this typed result.

    No PreparedCandidate or readiness is emitted. All model/rule/finality/fee/
    sizing/risk gates remain required by the independent candidate assembler.
    """
    from kalshi_predictor.data.schema import (
        AdvancedRiskDecisionLog,
        Forecast,
        PositionSizingDecisionLog,
    )

    expected_type = MiamiDevelopmentPreparationResult if _development else MiamiPreparationResult
    if type(result) is not expected_type or (_development and result.owned_storage is None):
        raise ValueError("COMPLETED_MIAMI_PREPARATION_REQUIRED")
    _isolated_connection(session, result.owned_storage)
    expected_state = "DEVELOPMENT_COMPUTED_UNQUALIFIED" if _development else "COMPUTED_UNQUALIFIED"
    if result.state != expected_state:
        raise ValueError("COMPLETED_MIAMI_PREPARATION_REQUIRED")
    if (
        result.engine_outputs is None
        or result.source_bundle is None
        or result.original_context is None
        or result.orderbook is None
        or result.orderbook_receipt is None
        or result.paper_eligible
        or result.execution_authority
    ):
        raise ValueError("MIAMI_HANDOFF_ORIGINALS_REQUIRED")
    records, engines = result.records, result.engine_outputs
    if result.owned_storage is not None:
        storage = result.owned_storage
        expected_storage = dict(
            database_path=str(storage.database_path),
            database_id=storage.authorization.database_id,
            generation=storage.owner.generation,
        )
        if canonical_hash(records.get("owned_storage")) != canonical_hash(expected_storage):
            raise ValueError("MIAMI_HANDOFF_STORAGE_BINDING")
    elif "owned_storage" in records:
        raise ValueError("MIAMI_HANDOFF_STORAGE_REQUIRED")
    at, current = _at(records["decision_at"]), _at(now)
    if not 0 <= (current - at).total_seconds() <= 60:
        raise ValueError("MIAMI_HANDOFF_STALE_OR_FUTURE")
    rebuilt = miami_source_bundle(result.original_context, decision_at=at)
    if canonical_hash(result.source_bundle) != canonical_hash(rebuilt) or records[
        "source_bundle_sha256"
    ] != canonical_hash(rebuilt):
        raise ValueError("MIAMI_HANDOFF_SOURCE_CHANGED")
    verified = verify_miami_provenance_source(rebuilt, decision_at=at, now=current)
    if (
        verified["inputs"]["ticker"] != result.ticker
        or engines.decision.ticker != result.ticker
        or engines.forecast_output.model_name != verified["inputs"]["model_name"]
        or engines.forecast_output.yes_probability
        != Decimal(verified["inputs"]["forecast_probability"])
    ):
        raise ValueError("MIAMI_HANDOFF_MODEL_IDENTITY")
    _receipt(
        result.orderbook,
        result.orderbook_receipt,
        result.original_context.market.url + "/orderbook",
    )
    if not 0 <= (current - _at(result.orderbook.received_at)).total_seconds() <= 60:
        raise ValueError("MIAMI_HANDOFF_BOOK_STALE")
    models = dict(
        forecast_id=Forecast,
        snapshot_id=MarketSnapshot,
        sizing_id=PositionSizingDecisionLog,
        risk_id=AdvancedRiskDecisionLog,
    )
    persisted: dict[str, Any] = {}
    for name, model in models.items():
        if type(records[name]) is not int or records[name] < 1:
            raise ValueError("MIAMI_HANDOFF_PERSISTED_ID_REQUIRED")
        row: Any = session.get(model, records[name])
        if row is None or row.ticker != result.ticker:
            raise ValueError("MIAMI_HANDOFF_PERSISTED_RECORD_MISSING")
        persisted[name] = row
    forecast = persisted["forecast_id"]
    stored_risk = json.loads(persisted["risk_id"].raw_json)
    if stored_risk["raw"].get("miami_records_sha256") != canonical_hash(
        json.loads(encode_json(records))
    ):
        raise ValueError("MIAMI_HANDOFF_RECORDS_CHANGED")
    if not (
        _at(records["forecast_generated_at"])
        <= _at(records["forecast_available_at"])
        <= at
        <= _at(records["computation_finished_at"])
        <= current
    ):
        raise ValueError("MIAMI_HANDOFF_AVAILABILITY_CLOCK")
    sizing_raw = json.loads(persisted["sizing_id"].raw_json)["raw"]
    if canonical_hash(sizing_raw["evidence"]) != canonical_hash(records["sizing_evidence"]):
        raise ValueError("MIAMI_HANDOFF_SIZING_EVIDENCE_CHANGED")
    stored_binding = json.loads(persisted["risk_id"].raw_json)["raw"]["miami_preparation"]
    expected_binding = dict(
        decision=asdict(engines.decision),
        risk_request=asdict(engines.risk_request),
        settings=records["settings"],
    )
    if canonical_hash(stored_binding) != canonical_hash(json.loads(encode_json(expected_binding))):
        raise ValueError("MIAMI_HANDOFF_DECISION_REQUEST_OR_SETTINGS_CHANGED")
    quote = decision_fee_quote(
        engines.decision.raw_decision_json,
        ticker=result.ticker,
        side=engines.decision.side,
        quantity=engines.decision.quantity,
        price=engines.decision.limit_price,
        simulator_floor=Decimal(str(records["settings"]["paper_default_fee_per_contract"])),
        now=current,
        required=True,
    )
    if (
        quote is None
        or canonical_hash(quote.decode()) != canonical_hash(records["fee_contract"])
        or engines.risk_request.estimated_round_trip_fees != quote.charge
        or stored_risk["raw"].get("guarded_fee_quote_sha256") != quote.sha256
        or stored_risk["raw"].get("estimated_round_trip_fees")
        != str(engines.risk_request.estimated_round_trip_fees)
    ):
        raise ValueError("MIAMI_HANDOFF_FEE_CHANGED")
    market = _decode(result.original_context.market.artifact)["market"]
    book = qualify_book(
        _decode(result.orderbook.artifact),
        received_at=_at(result.orderbook.received_at),
        now=_at(records["preparation_started_at"]),
        max_spread=Decimal(str(records["settings"]["opportunity_max_spread"])),
        liquidity_score=score_liquidity(
            volume=market.get("volume_fp"),
            open_interest=market.get("open_interest_fp"),
            liquidity=market.get("liquidity_dollars"),
        ),
        price_ranges=market.get("price_ranges"),
    )
    if canonical_hash(book) != canonical_hash(records["book_qualification"]):
        raise ValueError("MIAMI_HANDOFF_BOOK_QUALIFICATION_CHANGED")
    structure = _development_book_structure(book) if _development else None
    if structure is not None and canonical_hash(structure) != canonical_hash(
        records.get("development_book_structure")
    ):
        raise ValueError("MIAMI_DEVELOPMENT_BOOK_STRUCTURE_CHANGED")
    key = "YES" if engines.decision.side == BUY_YES else "NO"
    if (
        engines.decision.side not in {BUY_YES, BUY_NO}
        or (structure is None and not book["sides"][key]["executable"])
        or engines.decision.limit_price != Decimal(book["sides"][key]["ask"])
    ):
        raise ValueError("MIAMI_HANDOFF_EXECUTABLE_SIDE_CHANGED")
    ev = compute_net_ev(
        model_probability=engines.forecast_output.yes_probability
        if key == "YES"
        else 1 - engines.forecast_output.yes_probability,
        executable_price=engines.decision.limit_price,
        estimated_fee=quote.charge,
        slippage_allowance=Decimal(str(records["ev"]["slippage_allowance"])),
        uncertainty_buffer=Decimal(str(records["ev"]["uncertainty_buffer"])),
    )
    if canonical_hash(json.loads(encode_json(asdict(ev)))) != canonical_hash(
        json.loads(encode_json(records["ev"]))
    ):
        raise ValueError("MIAMI_HANDOFF_EV_CHANGED")
    if (
        engines.decision.forecast_id != forecast.id
        or forecast.model_name != engines.forecast_output.model_name
        or Decimal(forecast.yes_probability) != engines.forecast_output.yes_probability
        or canonical_hash(json.loads(forecast.feature_json))
        != canonical_hash(engines.forecast_output.feature_json)
        or _orm_at(forecast.forecasted_at) != _at(records["forecast_generated_at"])
        or canonical_hash(json.loads(encode_json(asdict(engines.forecast_output))))
        != canonical_hash(json.loads(encode_json(records["forecast"])))
    ):
        raise ValueError("MIAMI_HANDOFF_FORECAST_CHANGED")
    if _snapshot_id(session, result.ticker) != records["snapshot_id"] or canonical_hash(
        json.loads(persisted["snapshot_id"].raw_orderbook_json)
    ) != canonical_hash(_decode(result.orderbook.artifact)):
        raise ValueError("MIAMI_HANDOFF_SNAPSHOT_CHANGED")
    engine_records: tuple[tuple[str, PositionSizingDecision | AdvancedRiskDecision], ...] = (
        ("sizing", engines.phase3m),
        ("risk", engines.phase3n),
    )
    for name, value in engine_records:
        expected = value.as_dict()
        stored = json.loads(persisted[name + "_id"].raw_json)
        if (
            canonical_hash(records[name]) != canonical_hash(expected)
            or canonical_hash({k: stored.get(k) for k in expected}) != canonical_hash(expected)
            or _at(value.decision_timestamp) != at
        ):
            raise ValueError("MIAMI_HANDOFF_ENGINE_CHANGED")


def verify_miami_preparation_handoff(
    session: Session, result: MiamiPreparationResult, *, now: datetime
) -> None:
    _verify_miami_preparation_handoff(session, result, now=now)


def verify_miami_development_handoff(
    session: Session, result: MiamiDevelopmentPreparationResult, *, now: datetime
) -> None:
    _verify_miami_preparation_handoff(session, result, now=now, _development=True)


def prepare_owned_miami_development(
    session: Session,
    *,
    storage: MiamiOwnedStorage,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None,
) -> MiamiDevelopmentPreparationResult:
    """Generate genuine owned development rows; caller commits, no admission result."""
    verify_miami_storage(session, storage, now=utc_now())
    result = _prepare_miami_candidate(
        session,
        context=context,
        orderbook=orderbook,
        orderbook_receipt=orderbook_receipt,
        settings=settings,
        slippage_allowance=slippage_allowance,
        uncertainty_buffer=uncertainty_buffer,
        fee_evidence=fee_evidence,
        _storage=storage,
        _development=True,
    )
    if type(result) is not MiamiDevelopmentPreparationResult:
        raise ValueError("DEVELOPMENT_PREPARATION_TYPE_REQUIRED")
    return result


def prepare_miami_candidate(
    session: Session,
    *,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None,
) -> MiamiPreparationResult:
    """Original public API remains memory-only; never writes an arbitrary file."""
    return _prepare_miami_candidate(
        session,
        context=context,
        orderbook=orderbook,
        orderbook_receipt=orderbook_receipt,
        settings=settings,
        slippage_allowance=slippage_allowance,
        uncertainty_buffer=uncertainty_buffer,
        fee_evidence=fee_evidence,
    )


def prepare_owned_miami_candidate(
    session: Session,
    *,
    storage: MiamiOwnedStorage,
    context: MiamiGateContext,
    orderbook: MiamiOriginal,
    orderbook_receipt: Artifact,
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    fee_evidence: dict[str, Any] | None,
) -> MiamiPreparationResult:
    """Generate actual rows directly in the actively owned authorized file."""
    verify_miami_storage(session, storage, now=utc_now())
    return _prepare_miami_candidate(
        session,
        context=context,
        orderbook=orderbook,
        orderbook_receipt=orderbook_receipt,
        settings=settings,
        slippage_allowance=slippage_allowance,
        uncertainty_buffer=uncertainty_buffer,
        fee_evidence=fee_evidence,
        _storage=storage,
    )
