"""Owned-ledger prospective development observations; no admission candidate or orders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.paper import fees
from kalshi_predictor.utils.time import utc_now

from .dataset_store import persist_dataset_record
from .evaluation_dataset import _validate_stored_observation
from .miami_preparation import MiamiPreparationResult, verify_miami_preparation_handoff
from .miami_storage import MiamiOwnedStorage, verify_miami_storage
from .provenance import Artifact, canonical_hash
from .rule_verifier import RuleDocument
from .source_health import aware

TERMS_URL = "https://assets.kalshi.com/contract_terms/MIAWINDEX.pdf"
TERMS_SHA256 = "f7acb398f739ef0b4745e9ae4cbf19b9834676ca4316a81af5c7e3c2c4d20a87"
TERMS_RECEIPT_SHA256 = "52155cb05ff1bb5c76077dcc34d05f4469a95b484f16ac124bd86d4f6213acb9"
RULE_PROFILE = "MIAWINDEX_AT_ABOVE_OFFICIAL_RESULT_DEVELOPMENT_V1"
SEMANTICS = (
    "strict greater; original hundredth Fahrenheit; no re-rounding; "
    "latest canonical normal/degraded event minute in inclusive [target-60m,target]; "
    "determine no earlier than target+300s; first publication; missing/review unresolved; "
    "outcome only exact exchange finalized nonprovisional binary result; "
    "no independent first-publication reconstruction or finality deadline certification"
)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for key, value in pairs:
        if key in row:
            raise ValueError("DEVELOPMENT_DUPLICATE_JSON_KEY")
        row[key] = value
    return row


def _strict(artifact: Artifact) -> dict[str, Any]:
    artifact.decode()  # original raw-byte hash check
    row = json.loads(artifact.payload, object_pairs_hook=_unique)
    canonical_hash(row)  # rejects constants and exponent overflow
    return row


@dataclass(frozen=True)
class DevelopmentRuleBinding:
    """Original rule interpretation, deliberately not a CertifiedRulePolicy."""

    ticker: str
    event_id: str
    series: str
    observation_time: str
    receipt_sha256: str
    receipt_payload_hex: str
    profile: str = RULE_PROFILE
    semantics: str = SEMANTICS

    @property
    def version(self) -> str:
        return canonical_hash(asdict(self))

    def validate(
        self,
        identity: dict[str, Any],
        market: dict[str, Any],
        documents: tuple[RuleDocument, ...],
        at: datetime,
    ) -> None:
        if (
            self.profile != RULE_PROFILE
            or self.semantics != SEMANTICS
            or self.series != "KXTEMPMIAH"
            or any(getattr(self, key) != value for key, value in identity.items())
            or market.get("ticker") != self.ticker
            or market.get("event_ticker") != self.event_id
            or market.get("strike_type") != "greater"
            or market.get("market_type") != "binary"
            or market.get("cap_strike") is not None
            or aware(self.observation_time) <= aware(at)
            or aware(self.observation_time) != aware(market["close_time"])
        ):
            raise ValueError("DEVELOPMENT_RULE_IDENTITY_OR_PROFILE")
        threshold = Decimal(str(market.get("floor_strike")))
        if not threshold.is_finite() or threshold != threshold.quantize(Decimal(".01")):
            raise ValueError("DEVELOPMENT_RULE_PRECISION")
        if len(documents) != 1 or (documents[0].url, documents[0].sha256) != (
            TERMS_URL,
            TERMS_SHA256,
        ):
            raise ValueError("DEVELOPMENT_ORIGINAL_TERMS_REQUIRED")
        if self.receipt_sha256 != TERMS_RECEIPT_SHA256:
            raise ValueError("REVIEWED_DEVELOPMENT_TERMS_RECEIPT_REQUIRED")
        if len(self.receipt_payload_hex) > 8192:
            raise ValueError("DEVELOPMENT_RULE_RECEIPT_BUDGET")
        receipt = _strict(Artifact(self.receipt_sha256, bytes.fromhex(self.receipt_payload_hex)))
        if (
            receipt.get("url") != TERMS_URL
            or receipt.get("sha256") != TERMS_SHA256
            or receipt.get("method") != "GET"
            or type(receipt.get("status")) is not int
            or receipt["status"] != 200
            or type(receipt.get("bytes")) is not int
            or receipt["bytes"] != len(documents[0].payload)
            or not aware(receipt["requested_at"]) <= aware(receipt["received_at"]) <= aware(at)
        ):
            raise ValueError("DEVELOPMENT_ORIGINAL_TERMS_RECEIPT")


@dataclass(frozen=True)
class DevelopmentAppendResult:
    observation_sha256: str
    dataset_head_sha256: str
    recorded_at: str
    forecast_id: int
    snapshot_id: int
    database_id: str
    state: str = "DEVELOPMENT_RECORDED_UNQUALIFIED"
    admission_authority: bool = False
    orders_created: int = 0


def freeze_miami_development_protocol(
    *, storage: MiamiOwnedStorage, scenario_total: Decimal
) -> Artifact:
    """Record explicit scenario on the same owned ledger before acquisition.

    This is a trusted local writer clock, never an external timestamp attestation.
    The completed commit precedes return; callers must acquire new inputs afterward.
    """
    if (
        type(scenario_total) is not Decimal
        or not scenario_total.is_finite()
        or scenario_total < Decimal("1.01")
    ):
        raise ValueError("EXPLICIT_DEVELOPMENT_SCENARIO_REQUIRED")
    with Session(storage.engine) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            at = utc_now()
            verify_miami_storage(session, storage, now=at)
            row = dict(
                schema="miami-development-cost-protocol-v1",
                committed_at=at.isoformat(),
                uncertainty="COMMON_VACUOUS_PROBABILITY_BOUND",
                slippage="REVIEWED_M1_RATE007_DEBIT_SCENARIO_NOT_MEASURED",
                scenario_total=str(scenario_total),
                measurement=False,
                calibrated=False,
                selection="EXISTING_PREPARATION_SIDE_SELECTION",
            )
            raw = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            artifact = Artifact(hashlib.sha256(raw).hexdigest(), raw)
            session.execute(
                text(
                    "INSERT INTO overnight_sprint_cycles(id,captured_at,payload) "
                    "VALUES(:id,:at,:payload)"
                ),
                dict(
                    id="development-cost-protocol:" + artifact.sha256,
                    at=at.isoformat(),
                    payload=raw.decode(),
                ),
            )
            session.commit()
            return artifact
        except Exception:
            session.rollback()
            raise


def _verify_protocol_record(
    session: Session, protocol: Artifact, preparation: MiamiPreparationResult
) -> None:
    saved = session.execute(
        text("SELECT captured_at,payload FROM overnight_sprint_cycles WHERE id=:id"),
        dict(id="development-cost-protocol:" + protocol.sha256),
    ).one_or_none()
    if saved is None or saved.payload.encode() != protocol.payload:
        raise ValueError("SAME_LEDGER_DEVELOPMENT_PROTOCOL_REQUIRED")
    row = _strict(protocol)
    at = aware(saved.captured_at)
    context = preparation.original_context
    book = preparation.orderbook
    if context is None or book is None:
        raise ValueError("DEVELOPMENT_INPUT_RECEIPTS_REQUIRED")
    current = context.captures[context.current_capture]
    receipt = _strict(current.index_receipt)
    acquisition = aware(receipt.get("requested_at", current.index.received_at.isoformat()))
    if (
        at != aware(row["committed_at"])
        or at > aware(preparation.records["model_input_as_of"])
        or at > acquisition
        or any(
            at > aware(source.received_at)
            for source in (context.market, context.event, context.series, book)
        )
    ):
        raise ValueError("DEVELOPMENT_PROTOCOL_NOT_BEFORE_INPUT_RECEIPTS")


def _cost_protocol(
    protocol: Artifact, preparation: MiamiPreparationResult, settings: Settings
) -> dict:
    row = _strict(protocol)
    expected = {
        "schema": "miami-development-cost-protocol-v1",
        "committed_at": row.get("committed_at"),
        "uncertainty": "COMMON_VACUOUS_PROBABILITY_BOUND",
        "slippage": "REVIEWED_M1_RATE007_DEBIT_SCENARIO_NOT_MEASURED",
        "scenario_total": row.get("scenario_total"),
        "measurement": False,
        "calibrated": False,
        "selection": "EXISTING_PREPARATION_SIDE_SELECTION",
    }
    if canonical_hash(row) != canonical_hash(expected):
        raise ValueError("EXACT_DEVELOPMENT_COST_PROTOCOL_REQUIRED")
    if aware(row["committed_at"]) > aware(preparation.records["model_input_as_of"]):
        raise ValueError("DEVELOPMENT_COST_PROTOCOL_NOT_PROSPECTIVE")
    records = preparation.records
    p = Decimal(str(records["forecast"]["yes_probability"]))
    ev = records["ev"]
    price, fee, slip, uncertainty = (
        Decimal(str(ev[key]))
        for key in ("executable_price", "estimated_fee", "slippage_allowance", "uncertainty_buffer")
    )
    floor = settings.advanced_risk_gap_tail_buffer_per_contract
    slip_floor = settings.advanced_risk_estimated_slippage_per_contract
    if type(row["scenario_total"]) is not str:
        raise ValueError("DEVELOPMENT_DECIMAL_SCENARIO_REQUIRED")
    scenario = Decimal(row["scenario_total"])
    contract = records["fee_contract"]
    policies = [
        p
        for p in fees.CERTIFIED_FEE_POLICIES
        if p.version == contract["evidence"]["policy_version"]
    ]
    if len(policies) != 1 or preparation.original_context is None:
        raise ValueError("DEVELOPMENT_REVIEWED_FEE_REQUIRED")
    multiplier = preparation.original_context.series.artifact.decode()["series"]["fee_multiplier"]
    if (
        any(
            not value.is_finite()
            for value in (p, price, fee, slip, uncertainty, floor, slip_floor, scenario)
        )
        or not 0 <= p <= 1
        or not 0 <= floor <= 1
        or slip_floor < 0
        or not max(p, 1 - p, floor) <= uncertainty <= 1
        or scenario < max(Decimal("1.01"), 1 + settings.paper_default_fee_per_contract)
        or price + fee + slip < scenario
        or not slip_floor <= slip <= 1
        or Decimal(policies[0].taker_rate) != Decimal(".07")
        or Decimal(str(multiplier)) != 1
        or Decimal(str(ev["net_ev"])) > 0
    ):
        raise ValueError("DEVELOPMENT_CONSERVATIVE_COST_BOUND_REQUIRED")
    return row


def append_miami_development(
    *,
    preparation: MiamiPreparationResult,
    model: Artifact,
    model_code: bytes,
    settings: Settings,
    repository: Path,
    code_sha: str,
    rule: DevelopmentRuleBinding,
    rule_documents: tuple[RuleDocument, ...],
    cost_protocol: Artifact,
) -> DevelopmentAppendResult:
    """Atomically append genuine blocked preparation to its same owned file.

    Caller must retain the active runtime owner. No generic writer callback,
    candidate, activation, model policy registration or settlement acquisition.
    Recording time is the actual local append clock, not external attestation.
    """
    from .candidate_assembly import _assemble_candidate

    if type(preparation) is not MiamiPreparationResult or preparation.owned_storage is None:
        raise ValueError("OWNED_MIAMI_DEVELOPMENT_PREPARATION_REQUIRED")
    storage = preparation.owned_storage
    database_id = storage.authorization.database_id
    if not isinstance(database_id, str) or not database_id:
        raise ValueError("DEVELOPMENT_DATABASE_ID_REQUIRED")
    protocol = _cost_protocol(cost_protocol, preparation, settings)
    with Session(storage.engine) as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            at = utc_now()
            verify_miami_storage(session, storage, now=at)
            _verify_protocol_record(session, cost_protocol, preparation)
            verify_miami_preparation_handoff(session, preparation, now=at)
            artifact = _assemble_candidate(
                preparation=preparation,
                model=model,
                model_code=model_code,
                settings=settings,
                repository=repository,
                code_sha=code_sha,
                authorization=storage.authorization,
                rule_documents=rule_documents,
                now=at,
                miami_session=session,
                _development_rule=rule,
            )
            if type(artifact) is not Artifact:
                raise ValueError("DEVELOPMENT_OBSERVATION_ONLY_REQUIRED")
            row = artifact.decode()
            row["development"] = dict(
                status="UNQUALIFIED_NONTRADING",
                admission_authority=False,
                calibrated=False,
                rule_certified=False,
                finality_deadline=None,
                cost_protocol_sha256=cost_protocol.sha256,
                cost_protocol=protocol,
                additional_cost_scenario_not_measured=True,
                actual_scenario_debit=str(
                    Decimal(str(row["executable_price"]))
                    + Decimal(str(row["estimated_fee"]))
                    + Decimal(str(row["slippage"]))
                ),
            )
            raw = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            observation = Artifact(hashlib.sha256(raw).hexdigest(), raw)
            _validate_stored_observation(row)
            recorded_at = utc_now()
            if not 0 <= (
                recorded_at - aware(row["decision"]["decision_at"])
            ).total_seconds() <= 60 or recorded_at >= aware(row["event_window_end"]):
                raise ValueError("DEVELOPMENT_APPEND_CLOCK_INVALID")
            verify_miami_storage(session, storage, now=recorded_at)
            head = persist_dataset_record(
                session, dataset="paper-release", record=observation, recorded_at=recorded_at
            )
            session.commit()
            return DevelopmentAppendResult(
                observation.sha256,
                head,
                recorded_at.isoformat(),
                preparation.records["forecast_id"],
                preparation.records["snapshot_id"],
                database_id,
            )
        except Exception:
            session.rollback()
            raise
