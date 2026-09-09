"""Bounded public-result reconciliation for isolated LOCAL paper and shadow rows.

The adapter accepts captured public GET artifacts; it performs no network requests
and has no order-creation API. Every batch commits atomically or rolls back.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.data.repositories import upsert_settlement
from kalshi_predictor.data.schema import (
    Market,
    PaperFill,
    PaperOrder,
    PaperPnl,
    PaperPosition,
    Settlement,
)
from kalshi_predictor.overnight_paper.dataset_store import load_dataset, persist_dataset_record
from kalshi_predictor.overnight_paper.evaluation_dataset import (
    _validate_stored_observation,
    join_outcome,
    read_records,
)
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.settlement import market_lifecycle
from kalshi_predictor.overnight_paper.store import (
    aware,
    digest,
    encode,
    record_shadow_evaluation,
    score_final,
)
from kalshi_predictor.paper.ledger import mark_position_realized
from kalshi_predictor.paper.models import BUY_NO, BUY_YES, ORDER_FILLED
from kalshi_predictor.paper.pnl import calculate_settled_pnl

MAX_MARKETS = 3
MAX_PAYLOAD_BYTES = 1_000_000
MAX_SHADOWS_PER_BATCH = 1000
PUBLIC_HOSTS = frozenset({"external-api.kalshi.com", "api.elections.kalshi.com"})
FINAL_MARKER = "OVERNIGHT_PUBLIC_FINAL_V1"
PAPER_MARKER = "OVERNIGHT_PAPER_EVALUATION_V1"
MAX_DATASET_RECORDS = 10_000
MAX_DATASET_BYTES = 64 * 1024 * 1024


def _artifact(row: dict[str, Any]) -> Artifact:
    return Artifact(canonical_hash(row), encode(row).encode("utf-8"))


def _dataset_records(session: Session) -> tuple[dict[str, Artifact], dict[str, Artifact]]:
    count, size = session.execute(
        text(
            "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) "
            "FROM overnight_sprint_cycles WHERE id LIKE 'release-dataset:paper-release:%'"
        )
    ).one()
    if count > MAX_DATASET_RECORDS or size > MAX_DATASET_BYTES:
        raise ValueError("WATCHER_DATASET_VALIDATION_BUDGET_EXCEEDED")
    observations: dict[str, Artifact] = {}
    outcomes: dict[str, Artifact] = {}
    decision_ids: set[str] = set()
    for entry in read_records(load_dataset(session, dataset="paper-release")):
        row = entry["record"]
        record = _artifact(row)
        if row.get("kind") == "observation-v1":
            _validate_stored_observation(row)
            if (
                not 0
                <= (
                    aware(entry["recorded_at"]) - aware(row["decision"]["decision_at"])
                ).total_seconds()
                <= 60
            ):
                raise ValueError("DATASET_OBSERVATION_NOT_PROSPECTIVE")
            if record.sha256 in observations or row["decision_id"] in decision_ids:
                raise ValueError("DATASET_DUPLICATE_OBSERVATION")
            decision_ids.add(row["decision_id"])
            observations[record.sha256] = record
        elif row.get("kind") == "outcome-v1":
            key = row["observation_sha256"]
            if key not in observations or key in outcomes:
                raise ValueError("DATASET_OUTCOME_JOIN_CONFLICT")
            joined = join_outcome(
                observation=observations[key], outcome_artifact=_artifact(row["outcome"])
            )
            if joined.sha256 != record.sha256 or aware(entry["recorded_at"]) < aware(
                row["outcome"]["available_at"]
            ):
                raise ValueError("DATASET_OUTCOME_JOIN_CONFLICT")
            outcomes[key] = record
        elif row.get("kind") != "policy-v1":
            raise ValueError("DATASET_UNKNOWN_RECORD_KIND")
    return observations, outcomes


def pending_dataset_tickers(session: Session) -> frozenset[str]:
    observations, outcomes = _dataset_records(session)
    return frozenset(
        row.decode()["identity"]["ticker"]
        for key, row in observations.items()
        if key not in outcomes
    )


def _join_dataset_final(session: Session, observation: Artifact, now: datetime) -> None:
    row = observation.decode()
    marker = _cycle(session, "settlement-final:" + row["identity"]["ticker"])
    if marker is None:
        raise ValueError("DATASET_FINAL_MARKER_REQUIRED")
    final = verified_final_marker(marker, now=now)
    source = _source(marker["source"])
    market = source.market(now=now, enforce_fresh=False)
    if (
        market.get("event_ticker") != row["identity"]["event_id"]
        or market.get("series_ticker", row["identity"]["series"]) != row["identity"]["series"]
        or aware(market["close_time"]) != aware(row["decision"]["close_time"])
    ):
        raise ValueError("DATASET_FINAL_IDENTITY_MISMATCH")
    outcome = _artifact(
        dict(
            **row["identity"],
            result=final["result"],
            status="final",
            final_at=final["settled_at"],
            available_at=source.captured_at.isoformat(),
            source_url=source.source_url,
            provider_payload=json.loads(source.payload),
            provider_payload_sha256=canonical_hash(json.loads(source.payload)),
        )
    )
    joined = join_outcome(observation=observation, outcome_artifact=outcome)
    _, prior = _dataset_records(session)
    if observation.sha256 in prior and prior[observation.sha256].sha256 != joined.sha256:
        raise ValueError("DATASET_FINAL_OUTCOME_CONFLICT")
    persist_dataset_record(session, dataset="paper-release", record=joined, recorded_at=now)


@dataclass(frozen=True)
class PublicMarketObservation:
    ticker: str
    source_url: str
    captured_at: datetime
    sha256: str
    payload: bytes

    def market(self, *, now: datetime, enforce_fresh: bool = True) -> dict[str, Any]:
        parts = urlsplit(self.source_url)
        if (
            parts.scheme != "https"
            or parts.hostname not in PUBLIC_HOSTS
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or parts.query
            or parts.fragment
            or parts.path != "/trade-api/v2/markets/" + quote(self.ticker, safe="")
        ):
            raise ValueError("PUBLIC_MARKET_ENDPOINT_REQUIRED")
        if (
            not self.ticker
            or not 0 < len(self.payload) <= MAX_PAYLOAD_BYTES
            or hashlib.sha256(self.payload).hexdigest() != self.sha256
        ):
            raise ValueError("PUBLIC_MARKET_ARTIFACT_INTEGRITY")
        if (
            self.captured_at.tzinfo is None
            or now.tzinfo is None
            or self.captured_at > now
            or (enforce_fresh and (now - self.captured_at).total_seconds() > 60)
        ):
            raise ValueError("PUBLIC_MARKET_RECEIPT_STALE_OR_INVALID")
        payload = json.loads(self.payload)
        market = payload.get("market") if isinstance(payload, dict) else None
        if not isinstance(market, dict) or market.get("ticker") != self.ticker:
            raise ValueError("PUBLIC_MARKET_IDENTITY_MISMATCH")
        return market

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "source_url": self.source_url,
            "captured_at": self.captured_at.isoformat(),
            "sha256": self.sha256,
            "payload": self.payload.decode("utf-8"),
        }


@dataclass(frozen=True)
class WatcherReport:
    markets_observed: int
    shadow_evaluations_created: int
    paper_evaluations_created: int
    realized_paper_pnl: Decimal
    rows: tuple[dict[str, Any], ...]
    observation_ids: tuple[str, ...] = ()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source(data: dict[str, Any]) -> PublicMarketObservation:
    return PublicMarketObservation(
        data["ticker"],
        data["source_url"],
        aware(data["captured_at"]),
        data["sha256"],
        data["payload"].encode("utf-8"),
    )


def _cycle(session: Session, key: str) -> dict[str, Any] | None:
    raw = session.execute(
        text("SELECT payload FROM overnight_sprint_cycles WHERE id=:key"), {"key": key}
    ).scalar_one_or_none()
    return None if raw is None else json.loads(raw)


def _put_cycle(session: Session, key: str, payload: dict[str, Any], now: datetime) -> None:
    prior = _cycle(session, key)
    if prior is not None:
        if prior != payload:
            raise ValueError("WATCHER_CHECKPOINT_CONFLICT")
        return
    session.execute(
        text(
            "INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(:id,:at,:payload)"
        ),
        {"id": key, "at": now.isoformat(), "payload": encode(payload)},
    )


def verified_final_marker(marker: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    if marker.get("kind") != FINAL_MARKER:
        raise ValueError("FINAL_MARKER_KIND_INVALID")
    source = _source(marker["source"])
    market = source.market(now=now, enforce_fresh=False)
    state = market_lifecycle(source.ticker, market, now=source.captured_at)
    if state["final"] is None or state["final"] != marker.get("final"):
        raise ValueError("FINAL_MARKER_PROVENANCE_INVALID")
    _binary_value(market)
    return state["final"]


def _binary_value(market: dict[str, Any]) -> None:
    if "settlement_value_dollars" not in market or market["settlement_value_dollars"] is None:
        return
    value = Decimal(str(market["settlement_value_dollars"]))
    expected = Decimal(market.get("result") == "yes")
    if not value.is_finite() or value != expected:
        raise ValueError("BINARY_SETTLEMENT_VALUE_CONFLICT")


def _settlement_core(final: dict[str, Any]) -> tuple[str, str, str]:
    return final["ticker"], final["result"], aware(final["settled_at"]).isoformat()


def _persist_final(
    session: Session,
    observation: PublicMarketObservation,
    market: dict[str, Any],
    final: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    _binary_value(market)
    key = "settlement-final:" + observation.ticker
    marker = _cycle(session, key)
    existing = session.get(Settlement, observation.ticker)
    if marker is not None:
        prior = verified_final_marker(marker, now=now)
        if _settlement_core(prior) != _settlement_core(final):
            raise ValueError("FINAL_CORRECTION_REQUIRES_REVIEW")
        if (
            existing is None
            or existing.result != prior["result"]
            or existing.settled_at is None
            or _utc(existing.settled_at) != aware(prior["settled_at"])
            or Decimal(existing.yes_settlement_value or "NaN") != Decimal(prior["result"] == "yes")
        ):
            raise ValueError("FINAL_SETTLEMENT_RECONCILIATION_FAILED")
        # Later harmless metadata changes do not rewrite the initial source of evaluation.
        return prior
    if existing is not None:
        raise ValueError("UNVERIFIED_EXISTING_SETTLEMENT")
    upsert_settlement(session, market)
    _put_cycle(
        session, key, {"kind": FINAL_MARKER, "source": observation.as_dict(), "final": final}, now
    )
    return final


def verified_paper_marker(
    marker: dict[str, Any],
    *,
    final_marker: dict[str, Any],
    paper_order: Mapping[str, Any],
    paper_pnl: Mapping[str, Any],
    shadow_payload: dict[str, Any],
    shadow_order_id: int,
    shadow_evaluation: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    """Pure UI/read-model verification; unlinked/provisional P&L never means final."""
    final = verified_final_marker(final_marker, now=now)
    if (
        marker.get("kind") != PAPER_MARKER
        or marker.get("state") != "PAPER_EVALUATED"
        or marker.get("final") != final
        or marker.get("paper_order_id") != paper_order.get("id")
        or marker.get("paper_pnl_id") != paper_pnl.get("id")
        or paper_order.get("id") != shadow_order_id
        or marker.get("ticker") != paper_order.get("ticker")
        or marker.get("ticker") != paper_pnl.get("ticker")
        or paper_order.get("status") != ORDER_FILLED
        or paper_order.get("quantity") != 1
        or paper_order.get("side") not in {BUY_YES, BUY_NO}
        or paper_pnl.get("settlement_result") != final["result"]
        or marker.get("realized_paper_pnl") != paper_pnl.get("realized_pnl")
    ):
        raise ValueError("PAPER_MARKER_RECONCILIATION_FAILED")
    if (
        digest(shadow_payload) != marker.get("shadow_id")
        or shadow_payload.get("ticker") != marker.get("ticker")
        or shadow_payload.get("side") != paper_order.get("side")
        or shadow_payload.get("model") != paper_order.get("model_name")
        or Decimal(str(shadow_payload["price"])) != Decimal(str(paper_order["limit_price"]))
    ):
        raise ValueError("PAPER_SHADOW_LINK_RECONCILIATION_FAILED")
    market = _source(final_marker["source"]).market(now=now, enforce_fresh=False)
    if (
        market.get("event_ticker") != shadow_payload["event_ticker"]
        or market.get("series_ticker", shadow_payload["series_ticker"])
        != shadow_payload["series_ticker"]
        or aware(market["close_time"]) != aware(shadow_payload["close_time"])
    ):
        raise ValueError("PAPER_FINAL_IDENTITY_MISMATCH")
    scored = score_final(shadow_payload, final)
    if shadow_evaluation != scored or any(
        marker.get("evaluation", {}).get(key) != value for key, value in scored.items()
    ):
        raise ValueError("PAPER_SHADOW_EVALUATION_RECONCILIATION_FAILED")
    fees = Decimal(marker["actual_simulated_fees"])
    from kalshi_predictor.paper.fees import CONTRACT_KEY, historical_fee_quote

    contract = shadow_payload.get("qualification_inputs", {}).get(CONTRACT_KEY)
    if contract is not None:
        quote = historical_fee_quote(
            contract,
            ticker=paper_order["ticker"],
            side=paper_order["side"],
            quantity=paper_order["quantity"],
            price=Decimal(str(paper_order["limit_price"])),
        )
        if (
            fees != quote.charge
            or marker.get("fee_quote_sha256") != quote.sha256
            or marker.get("fee_provenance") != "GUARDED_FEE_EVIDENCE_V1"
        ):
            raise ValueError("PAPER_MARKER_FEE_LINEAGE_MISMATCH")
    won = (paper_order["side"] == BUY_YES) == (final["result"] == "yes")
    calculated = Decimal(won) - Decimal(str(paper_order["limit_price"])) - fees
    if not fees.is_finite() or fees < 0 or calculated != Decimal(marker["realized_paper_pnl"]):
        raise ValueError("PAPER_REALIZED_ARITHMETIC_MISMATCH")
    return marker


def verified_settled_tickers(session: Session, *, now: datetime) -> frozenset[str]:
    """Capacity excludes only paper results bound to a verified finalized public artifact.

    A provisional/unlinked PaperPnl or Settlement row is never sufficient.
    """
    rows = session.execute(
        text("SELECT payload FROM overnight_sprint_cycles WHERE id LIKE 'paper-evaluation:%'")
    ).scalars()
    result: set[str] = set()
    for raw in rows:
        marker = json.loads(raw)
        if marker.get("kind") != PAPER_MARKER:
            raise ValueError("PAPER_EVALUATION_MARKER_INVALID")
        final_marker = _cycle(session, "settlement-final:" + marker["ticker"])
        if final_marker is None:
            raise ValueError("PAPER_FINAL_PROVENANCE_MISSING")
        pnl = session.get(PaperPnl, marker["paper_pnl_id"])
        order = session.get(PaperOrder, marker["paper_order_id"])
        shadow = session.execute(
            text(
                "SELECT payload,paper_order_id,evaluation_json FROM overnight_shadow WHERE id=:id"
            ),
            {"id": marker["shadow_id"]},
        ).first()
        if pnl is None or order is None or shadow is None or shadow[2] is None:
            raise ValueError("PAPER_FINAL_LINKAGE_MISSING")
        verified_paper_marker(
            marker,
            final_marker=final_marker,
            paper_order={
                "id": order.id,
                "ticker": order.ticker,
                "status": order.status,
                "quantity": order.quantity,
                "side": order.side,
                "limit_price": order.limit_price,
                "model_name": order.model_name,
            },
            paper_pnl={
                "id": pnl.id,
                "ticker": pnl.ticker,
                "settlement_result": pnl.settlement_result,
                "realized_pnl": pnl.realized_pnl,
            },
            shadow_payload=json.loads(shadow[0]),
            shadow_order_id=shadow[1],
            shadow_evaluation=json.loads(shadow[2]),
            now=now,
        )
        result.add(marker["ticker"])
    return frozenset(result)


def _evaluate_paper(
    session: Session,
    shadow_id: str,
    payload: dict[str, Any],
    order_id: int,
    final: dict[str, Any],
    now: datetime,
) -> tuple[dict[str, Any], bool]:
    order = session.get(PaperOrder, order_id)
    if (
        order is None
        or order.ticker != payload["ticker"]
        or order.side != payload["side"]
        or order.model_name != payload["model"]
        or order.quantity != 1
        or order.side not in {BUY_YES, BUY_NO}
        or Decimal(order.probability) != Decimal(str(payload["forecast"]))
    ):
        raise ValueError("PAPER_SHADOW_IDENTITY_MISMATCH")
    if order.status != ORDER_FILLED:
        return {
            "ticker": order.ticker,
            "state": "FINAL_RESULT_AVAILABLE",
            "blocker": "LOCAL_ORDER_NOT_FILLED",
            "paper_order_id": order_id,
        }, False
    existing = _cycle(session, f"paper-evaluation:{order_id}")
    if existing is not None:
        if order.ticker not in verified_settled_tickers(session, now=now):
            raise ValueError("PAPER_EVALUATION_UNVERIFIED")
        return existing, False
    fills = list(session.scalars(select(PaperFill).where(PaperFill.paper_order_id == order_id)))
    position = session.get(PaperPosition, order.ticker)
    order_count = session.execute(
        text("SELECT count(*) FROM paper_orders WHERE ticker=:ticker"), {"ticker": order.ticker}
    ).scalar_one()
    if (
        len(fills) != 1
        or position is None
        or order_count != 1
        or fills[0].quantity != 1
        or fills[0].ticker != order.ticker
        or fills[0].side != order.side
        or Decimal(fills[0].price) != Decimal(order.limit_price)
        or position.yes_contracts != int(order.side == BUY_YES)
        or position.no_contracts != int(order.side == BUY_NO)
    ):
        raise ValueError("PAPER_FILL_POSITION_RECONCILIATION_FAILED")
    expected_price = position.avg_yes_price if order.side == BUY_YES else position.avg_no_price
    if Decimal(expected_price or "NaN") != Decimal(fills[0].price):
        raise ValueError("PAPER_POSITION_COST_MISMATCH")
    fill_price = Decimal(fills[0].price)
    if not fill_price.is_finite() or not 0 < fill_price < 1:
        raise ValueError("PAPER_FILL_PRICE_INVALID")
    fee = Decimal(fills[0].fee)
    if not fee.is_finite() or fee < 0:
        raise ValueError("PAPER_FEE_INVALID")
    from kalshi_predictor.paper.fees import CONTRACT_KEY, historical_fee_quote

    contract = payload.get("qualification_inputs", {}).get(CONTRACT_KEY)
    order_contract = json.loads(order.raw_decision_json).get(CONTRACT_KEY)
    if contract != order_contract:
        raise ValueError("PAPER_FEE_SHADOW_ORDER_MISMATCH")
    fee_quote = None
    if contract is not None:
        fee_quote = historical_fee_quote(
            contract,
            ticker=order.ticker,
            side=order.side,
            quantity=order.quantity,
            price=fill_price,
        )
        fill_raw = json.loads(fills[0].raw_fill_json)
        if (
            fee != fee_quote.charge
            or fill_raw.get("fee_contract") != contract
            or fill_raw.get("fee_quote_sha256") != fee_quote.sha256
            or fill_raw.get("fee_provenance") != "GUARDED_FEE_EVIDENCE_V1"
        ):
            raise ValueError("PAPER_FEE_FILL_LINEAGE_MISMATCH")
    realized = calculate_settled_pnl(position, final["result"], fees=fee)
    if realized is None or not realized.is_finite():
        raise ValueError("PAPER_PNL_UNDEFINED")
    prior_pnl = session.scalar(
        select(PaperPnl).where(
            PaperPnl.ticker == order.ticker, PaperPnl.settlement_result.is_not(None)
        )
    )
    if prior_pnl is not None:
        raise ValueError("UNVERIFIED_EXISTING_REALIZED_PNL")
    mark_position_realized(session, position, realized)
    pnl = PaperPnl(
        ticker=order.ticker,
        calculated_at=now,
        yes_contracts=position.yes_contracts,
        no_contracts=position.no_contracts,
        avg_yes_price=position.avg_yes_price,
        avg_no_price=position.avg_no_price,
        settlement_result=final["result"],
        realized_pnl=str(realized),
        unrealized_pnl="0",
        total_pnl=str(realized),
        notes="verified overnight local paper final settlement",
    )
    session.add(pnl)
    session.flush()
    evaluation = score_final(payload, final)
    evaluation.update(
        model_version=payload["model_version"],
        category=payload.get("qualification_inputs", {}).get("category"),
        predicted_net_ev=payload["net_ev"],
        forecast_correct=(Decimal(str(payload["forecast"])) >= Decimal("0.5"))
        == (final["result"] == "yes"),
    )
    marker = {
        "kind": PAPER_MARKER,
        "ticker": order.ticker,
        "state": "PAPER_EVALUATED",
        "transitions": ["FINAL_RESULT_AVAILABLE", "PAPER_SETTLED", "PAPER_EVALUATED"],
        "shadow_id": shadow_id,
        "paper_order_id": order.id,
        "paper_pnl_id": pnl.id,
        "realized_paper_pnl": str(realized),
        "actual_simulated_fees": str(fee),
        "fee_provenance": (
            "GUARDED_FEE_EVIDENCE_V1" if fee_quote is not None else "LEGACY_CONFIGURED_NONCERTIFIED"
        ),
        "fee_quote_sha256": None if fee_quote is None else fee_quote.sha256,
        "final": final,
        "evaluation": evaluation,
    }
    _put_cycle(session, f"paper-evaluation:{order.id}", marker, now)
    return marker, True


def reconcile_public_settlements(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    observations: tuple[PublicMarketObservation, ...],
    now: datetime,
) -> WatcherReport:
    if not 0 < len(observations) <= MAX_MARKETS or len({o.ticker for o in observations}) != len(
        observations
    ):
        raise ValueError("WATCHER_MARKET_BATCH_LIMIT")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)()
        for p in (database_path, *database_path.parents)
    ):
        raise ValueError("ISOLATED_UNLINKED_DATABASE_REQUIRED")
    parsed = [(item, item.market(now=now)) for item in observations]
    shadow_count = paper_count = 0
    realized_total = Decimal("0")
    output: list[dict[str, Any]] = []
    observation_ids: list[str] = []
    with session_factory() as session:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            dbs = session.execute(text("PRAGMA database_list")).all()
            main = [row for row in dbs if row[1] == "main"]
            if (
                len(main) != 1
                or Path(main[0][2]).resolve() != path
                or any(row[1] not in {"main", "temp"} for row in dbs)
            ):
                raise ValueError("DATABASE_PATH_MISMATCH")
            if session.execute(text("PRAGMA integrity_check")).scalar() != "ok":
                raise ValueError("DATABASE_INTEGRITY_FAILED")
            raw_db = session.connection().connection.driver_connection
            if not isinstance(raw_db, sqlite3.Connection):
                raise ValueError("SQLITE_SINGLE_WRITER_REQUIRED")
            dataset_observations, _ = _dataset_records(session)
            for observation, market in parsed:
                originals = [
                    item
                    for item in dataset_observations.values()
                    if item.decode()["identity"]["ticker"] == observation.ticker
                ]
                shadows = session.execute(
                    text(
                        "SELECT id,payload,paper_order_id,evaluation_json FROM overnight_shadow "
                        "WHERE ticker=:ticker LIMIT :limit"
                    ),
                    {"ticker": observation.ticker, "limit": MAX_SHADOWS_PER_BATCH + 1},
                ).all()
                if (not shadows and not originals) or len(shadows) + len(
                    originals
                ) > MAX_SHADOWS_PER_BATCH:
                    raise ValueError("WATCHER_TRACKED_SHADOW_BATCH_REQUIRED")
                state = market_lifecycle(observation.ticker, market, now=observation.captured_at)
                local_market = session.get(Market, observation.ticker)
                if local_market is None:
                    raise ValueError("LOCAL_MARKET_IDENTITY_REQUIRED")
                for original in originals:
                    decision = original.decode()["decision"]
                    if (
                        decision["event_id"] != local_market.event_ticker
                        or decision["series"] != local_market.series_ticker
                        or market.get("event_ticker") != decision["event_id"]
                        or market.get("series_ticker", decision["series"]) != decision["series"]
                        or aware(market["close_time"]) != aware(decision["close_time"])
                    ):
                        raise ValueError("EXACT_SETTLEMENT_CONTRACT_IDENTITY_REQUIRED")
                if state["final"] is not None and originals:
                    _persist_final(session, observation, market, state["final"], now)
                    for original in originals:
                        _join_dataset_final(session, original, now)
                if not shadows:
                    if (
                        state["final"] is None
                        and _cycle(session, "settlement-final:" + observation.ticker) is not None
                    ):
                        raise ValueError("FINAL_LIFECYCLE_REGRESSION_REQUIRES_REVIEW")
                    output.append(
                        {
                            "ticker": observation.ticker,
                            "state": "DATASET_OUTCOME_RECORDED"
                            if state["final"] is not None
                            else state["state"],
                        }
                    )
                for shadow_id, raw_payload, order_id, prior_evaluation in shadows:
                    payload = json.loads(raw_payload)
                    if digest(payload) != shadow_id:
                        raise ValueError("SHADOW_PAYLOAD_INTEGRITY")
                    if (
                        payload["ticker"] != observation.ticker
                        or payload["event_ticker"] != market.get("event_ticker")
                        or local_market.event_ticker != payload["event_ticker"]
                        or local_market.series_ticker != payload["series_ticker"]
                        or market.get("series_ticker", payload["series_ticker"])
                        != payload["series_ticker"]
                        or aware(payload["close_time"]) != aware(market["close_time"])
                    ):
                        raise ValueError("EXACT_SETTLEMENT_CONTRACT_IDENTITY_REQUIRED")
                    if state["final"] is None:
                        if _cycle(session, "settlement-final:" + observation.ticker) is not None:
                            raise ValueError("FINAL_LIFECYCLE_REGRESSION_REQUIRES_REVIEW")
                        output.append(
                            {
                                "ticker": observation.ticker,
                                "shadow_id": shadow_id,
                                "state": state["state"],
                            }
                        )
                        continue
                    final = _persist_final(session, observation, market, state["final"], now)
                    record_shadow_evaluation(raw_db, shadow_id, final)
                    shadow_count += int(prior_evaluation is None)
                    if order_id is not None:
                        marker, created = _evaluate_paper(
                            session, shadow_id, payload, order_id, final, now
                        )
                        paper_count += int(created)
                        if created:
                            realized_total += Decimal(marker["realized_paper_pnl"])
                        output.append(marker)
                    else:
                        output.append(
                            {
                                "ticker": observation.ticker,
                                "shadow_id": shadow_id,
                                "state": "SHADOW_EVALUATED",
                                "final": final,
                            }
                        )
                receipt = {
                    "kind": "OVERNIGHT_WATCHER_OBSERVATION_V1",
                    "source": observation.as_dict(),
                    "lifecycle": state,
                }
                receipt_id = "watcher-observation:" + digest(receipt)
                _put_cycle(session, receipt_id, receipt, now)
                observation_ids.append(receipt_id)
            session.commit()
            return WatcherReport(
                len(observations),
                shadow_count,
                paper_count,
                realized_total,
                tuple(output),
                tuple(observation_ids),
            )
        except Exception:
            session.rollback()
            raise
