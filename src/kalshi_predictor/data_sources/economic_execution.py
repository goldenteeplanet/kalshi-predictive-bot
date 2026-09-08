"""Actual existing-model computation in an ephemeral caller-owned research session.

No HTTP, commits, persistent ledger or admission. A receipt binds recorded inputs
and execution, not provider authenticity, calibration or atomic code immutability.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings
from kalshi_predictor.data.schema import (
    EconomicEvent,
    EconomicFeature,
    EconomicMarketLink,
    Market,
    MarketSnapshot,
)
from kalshi_predictor.economic.features import calculate_economic_features
from kalshi_predictor.economic.repository import (
    insert_economic_feature,
    insert_economic_market_link,
)
from kalshi_predictor.economic.source_research import build_economic_source_features
from kalshi_predictor.forecasting.economic_v1 import EconomicV1Forecaster
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.overnight_paper.boundary_gate import audit_local_call_path
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.research.fred import FREDResponse
from kalshi_predictor.utils.time import utc_now

ENTRYPOINT = "kalshi_predictor.forecasting.economic_v1:EconomicV1Forecaster.forecast"
METHODS = {"off": "same_book_midpoint", "on": ENTRYPOINT}
CONTRAST = "MARKET_BASELINE_VS_LAGGED_SA_CPI_MOMENTUM_ECONOMIC_V1"
TERMS_SHA256 = "2317b1d8e823082b409f6ff3415fb135804d9682681f9f92f640b3681b29a872"
BASE = "https://external-api.kalshi.com/trade-api/v2"


def _json(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("ECONOMIC_NONFINITE_VALUE")
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json(item) for item in value]
    if value is None or isinstance(value, str | bool | int | float):
        return value
    raise ValueError("ECONOMIC_UNSERIALIZABLE_VALUE")


def _artifact(value: Any) -> Artifact:
    raw = json.dumps(_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


def economic_model_code_bundle(repository: Path) -> tuple[dict[str, str], bytes]:
    """Fingerprint the actual model and source bridge; not an admission audit.

    The bridge imports response types from modules which also define HTTP
    clients. The audit's findings are retained literally, never waived into a
    boundary PASS. This function and execution do not instantiate those clients.
    """
    root = repository.resolve(strict=True)
    audit = audit_local_call_path(
        root,
        entrypoints=(
            ("kalshi_predictor.forecasting.economic_v1", "EconomicV1Forecaster"),
            ("kalshi_predictor.economic.source_research", "build_economic_source_features"),
            ("kalshi_predictor.economic.features", "calculate_economic_features"),
        ),
    )
    if not audit.source_hashes:
        raise ValueError("ECONOMIC_DEPENDENCIES_UNAVAILABLE")
    sources = []
    dependencies = {}
    for module, digest in audit.source_hashes:
        stem = root / "src" / module.replace(".", "/")
        path = stem.with_suffix(".py")
        if not path.is_file():
            path = stem / "__init__.py"
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root / "src"):
            raise ValueError("ECONOMIC_DEPENDENCY_PATH_INVALID")
        raw = resolved.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("ECONOMIC_DEPENDENCY_CHANGED")
        relative = path.relative_to(root).as_posix()
        dependencies[relative] = digest
        sources.append(dict(module=module, path=relative, sha256=digest, source=raw.decode()))
    own = Path(__file__).resolve()
    if not own.is_relative_to(root / "src"):
        raise ValueError("ECONOMIC_EXECUTOR_ORIGIN_INVALID")
    raw = own.read_bytes().replace(b"\r\n", b"\n")
    relative = own.relative_to(root).as_posix()
    dependencies[relative] = hashlib.sha256(raw).hexdigest()
    sources.append(
        dict(module=__name__, path=relative, sha256=dependencies[relative], source=raw.decode())
    )
    bundle = _artifact(
        dict(
            schema="economic-research-source-bundle-v1",
            entrypoint=ENTRYPOINT,
            sources=sources,
            boundary_audit_passed=audit.passed,
            boundary_audit_blockers=list(audit.blockers),
            runtime_versions={
                name: version(name) for name in ("SQLAlchemy", "pydantic", "pydantic-settings")
            },
            python_version=sys.version,
            scope="RESEARCH_DEPENDENCY_FINGERPRINT_NOT_ADMISSION_BOUNDARY",
        )
    )
    return dependencies, bundle.payload


def _frozen(
    model: Artifact, procedure: Artifact, code: bytes, settings: Settings, root: Path, at: datetime
) -> dict[str, str]:
    if not 0 < len(model.payload) <= 2_000_000 or not 0 < len(procedure.payload) <= 1_000_000:
        raise ValueError("ECONOMIC_MANIFEST_SIZE_INVALID")
    if not 0 < len(code) <= 5_000_000:
        raise ValueError("ECONOMIC_CODE_SIZE_INVALID")
    m, p = model.decode(), procedure.decode()
    config = settings.model_dump(mode="json")
    if any(
        config[key]
        for key in (
            "kalshi_api_key_id",
            "kalshi_private_key_path",
            "postgres_password",
            "execution_confirmation_token",
        )
    ):
        raise ValueError("ECONOMIC_CREDENTIAL_FREE_SETTINGS_REQUIRED")
    if (
        m["name"] != "economic_v1"
        or not m["version"]
        or m["model_kind"] != "fixed_heuristic"
        or m["training_cutoff"] is not None
        or m["training_dataset_hashes"] != []
        or m.get("training_artifacts", []) != []
        or m["parameters"] != config
        or m["parameters_sha256"] != canonical_hash(config)
        or m["model_entrypoint"] != ENTRYPOINT
        or m["code_sha256"] != hashlib.sha256(code).hexdigest()
        or p["kind"] != "lagged-cpi-procedure-v1"
        or not p["name"]
        or not p["version"]
        or p["model_artifact_sha256"] != model.sha256
        or p["settings_sha256"] != canonical_hash(config)
        or p["variant_methods"] != METHODS
        or p["contrast_type"] != CONTRAST
    ):
        raise ValueError("ECONOMIC_FROZEN_MANIFEST_INVALID")
    for row in (m, p):
        if (
            not aware(row["created_at"])
            <= aware(row["frozen_at"])
            <= aware(row["available_at"])
            <= at
        ):
            raise ValueError("ECONOMIC_FROZEN_CLOCK_INVALID")
    if aware(m["available_at"]) > aware(p["frozen_at"]):
        raise ValueError("ECONOMIC_MODEL_AFTER_PROCEDURE_FREEZE")
    dependencies, current = economic_model_code_bundle(root)
    if current != code or dependencies != m["code_dependencies"]:
        raise ValueError("ECONOMIC_FROZEN_CODE_CHANGED")
    for source in json.loads(current)["sources"]:
        loaded = sys.modules.get(source["module"])
        if loaded is not None:
            origin = getattr(loaded, "__file__", None)
            if origin is None or Path(origin).resolve() != (root / source["path"]).resolve():
                raise ValueError("ECONOMIC_LOADED_ORIGIN_CHANGED")
    return dependencies


def _capture(
    original: Artifact, url: str, frozen_at: datetime, at: datetime, max_age: int
) -> dict[str, Any]:
    if not 0 < len(original.payload) <= 1_000_000:
        raise ValueError("ECONOMIC_CAPTURE_SIZE_INVALID")
    row = original.decode()
    if (
        row["url"] != url
        or not isinstance(row["body"], dict)
        or not frozen_at <= aware(row["received_at"]) <= at
        or (at - aware(row["received_at"])).total_seconds() > max_age
    ):
        raise ValueError("ECONOMIC_CAPTURE_IDENTITY_OR_CLOCK_INVALID")
    return row


def execute_lagged_cpi_research(
    *,
    session: Session,
    frozen_model: Artifact,
    frozen_procedure: Artifact,
    model_code: bytes,
    repository: Path,
    settings: Settings,
    source: FREDResponse,
    market: Artifact,
    event: Artifact,
    series: Artifact,
    book: Artifact,
    contract_terms: bytes,
) -> Artifact:
    """Flush existing research rows under SAVEPOINT; never commit caller work.

    Only an in-memory SQLite Session with an already-open caller transaction and
    no pending changes is accepted. IDs in the receipt are ephemeral research
    IDs. The caller must separately preserve the returned original receipt.
    """
    if type(session) is not Session or type(settings) is not Settings:
        raise ValueError("ECONOMIC_CONCRETE_SESSION_SETTINGS_REQUIRED")
    # SQLAlchemy's default bind does not constrain ORM routing: mapper and
    # Table binds can silently target another engine. Reject the entire bind
    # registry before resolving any connection. Unknown registry layout also
    # fails closed; SQLAlchemy's runtime version is frozen in the code bundle.
    alternate_binds = vars(session).get("_Session__binds")
    if not isinstance(alternate_binds, dict) or alternate_binds:
        raise ValueError("ECONOMIC_ALTERNATE_SESSION_BINDS_REFUSED")
    engine = session.get_bind()
    if (
        not isinstance(engine, Engine)
        or engine.dialect.name != "sqlite"
        or engine.url.database not in {None, "", ":memory:"}
        or engine.url.query
        or not session.in_transaction()
        or session.new
        or session.dirty
        or session.deleted
    ):
        raise ValueError("ECONOMIC_EPHEMERAL_CALLER_TRANSACTION_REQUIRED")
    connection = session.connection()
    driver = connection.connection.driver_connection
    if type(driver) is not sqlite3.Connection or any(
        row[1] not in {"main", "temp"} or row[2]
        for row in connection.exec_driver_sql("PRAGMA database_list")
    ):
        raise ValueError("ECONOMIC_ATTACHED_OR_NONMEMORY_DATABASE_REFUSED")
    # SQLite legacy transaction mode otherwise releases a standalone SAVEPOINT
    # durably, defeating the caller's later rollback despite Session.begin().
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN")
    started = utc_now()
    root = repository.resolve(strict=True)
    before = _frozen(frozen_model, frozen_procedure, model_code, settings, root, started)
    p = frozen_procedure.decode()
    freeze = aware(p["available_at"])
    mapping = p["contract_mapping"]
    if (
        mapping["series_ticker"] != "KXCPI"
        or mapping["source_series"] != "CPIAUCSL"
        or mapping["concept"] != "headline_cpi_u"
        or mapping["seasonal_adjustment"] != "SA"
        or mapping["outcome_units"] != "published_one_decimal_monthly_percent_change"
        or mapping["comparator"] != "strictly_greater"
        or mapping["revision_policy"] != "exclude_revisions_after_expiration"
        or mapping["contract_terms_sha256"] != TERMS_SHA256
        or hashlib.sha256(contract_terms).hexdigest() != TERMS_SHA256
    ):
        raise ValueError("ECONOMIC_EXACT_RESEARCH_MAPPING_REQUIRED")
    target = date.fromisoformat(mapping["target_month"] + "-01")
    release = aware(mapping["scheduled_release_at"])
    ticker = p["ticker"]
    if mapping["event_ticker"] != "KXCPI-" + target.strftime("%y%b").upper():
        raise ValueError("ECONOMIC_TARGET_EVENT_INVALID")
    if not started < release or release.date() <= target:
        raise ValueError("ECONOMIC_PRE_RELEASE_ONLY")
    originals = [
        _capture(market, f"{BASE}/markets/{ticker}", freeze, started, 60),
        _capture(series, f"{BASE}/series/KXCPI", freeze, started, 60),
        _capture(book, f"{BASE}/markets/{ticker}/orderbook", freeze, started, 60),
        _capture(event, f"{BASE}/events/{mapping['event_ticker']}", freeze, started, 60),
    ]
    event_row = originals[3]["body"]["event"]
    if (
        event_row["event_ticker"] != mapping["event_ticker"]
        or event_row["series_ticker"] != "KXCPI"
        or event_row.get("fee_type_override") is not None
        or event_row.get("fee_multiplier_override") is not None
    ):
        raise ValueError("ECONOMIC_EVENT_OR_FEE_OVERRIDE_UNSUPPORTED")
    m, s = originals[0]["body"]["market"], originals[1]["body"]["series"]
    strike = Decimal(str(m["floor_strike"]))
    expected_rule = (
        f"If the Consumer Price Index (CPI) increases by more than {strike:.1f}% "
        f"(single-decimal) in {target.strftime('%B %Y')}, then the market resolves to Yes."
    )
    if (
        m["ticker"] != ticker
        or m["event_ticker"] != mapping["event_ticker"]
        or m.get("series_ticker", "KXCPI") != "KXCPI"
        or s["ticker"] != "KXCPI"
        or m["strike_type"] != "greater"
        or not strike.is_finite()
        or strike != Decimal(str(mapping["strike_percent"]))
        or m["rules_primary"] != expected_rule
        or m["status"] not in {"open", "active"}
        or not started < aware(m["close_time"]) <= release
        or s["contract_terms_url"] != "https://assets.kalshi.com/contract_terms/CPI.pdf"
    ):
        raise ValueError("ECONOMIC_MARKET_MAPPING_DISCREPANCY")
    if type(source) is not FREDResponse or source.series_id != "CPIAUCSL":
        raise ValueError("ECONOMIC_SA_CPI_SOURCE_REQUIRED")
    if not freeze <= aware(source.original.received_at) <= started:
        raise ValueError("ECONOMIC_SOURCE_BEFORE_FREEZE_OR_FUTURE")
    inputs = build_economic_source_features(source, decision_at=started)
    if (
        len(inputs.observations) != 2
        or inputs.momentum_score is None
        or inputs.momentum_status != "ACTUAL_VS_PREVIOUS_MOMENTUM_NOT_CONSENSUS_SURPRISE"
        or [x.observation_date.isoformat() for x in inputs.observations] != p["lagged_periods"]
        or any(x.observation_date >= target for x in inputs.observations)
        or (target.year - inputs.observations[-1].observation_date.year) * 12
        + target.month
        - inputs.observations[-1].observation_date.month
        != 1
    ):
        raise ValueError("ECONOMIC_EXACT_TWO_LAGGED_MONTHS_REQUIRED")
    parsed = parse_orderbook(originals[2]["body"])
    bid, ask = parsed.best_yes_bid, parsed.best_yes_ask
    if bid is None or ask is None or not 0 <= bid < ask <= 1:
        raise ValueError("ECONOMIC_TWO_SIDED_BOOK_REQUIRED")
    midpoint = (bid + ask) / 2
    baseline_at = utc_now()
    event_key = "lagged_cpi_" + frozen_procedure.sha256
    with session.begin_nested():
        if (
            session.scalar(select(EconomicFeature.id).where(EconomicFeature.event_key == event_key))
            is not None
        ):
            raise ValueError("ECONOMIC_PROCEDURE_ALREADY_EXECUTED")
        if (
            session.scalar(select(EconomicMarketLink.id).where(EconomicMarketLink.ticker == ticker))
            is not None
        ):
            raise ValueError("ECONOMIC_EXISTING_LINK_AMBIGUOUS")
        lagged_event = EconomicEvent(
            event_key=event_key,
            category="cpi",
            actual_value=inputs.observations[1].raw_value,
            previous_value=inputs.observations[0].raw_value,
            forecast_value=None,
        )
        calculated = calculate_economic_features(lagged_event)
        if calculated["surprise_score"] != inputs.momentum_score:
            raise ValueError("ECONOMIC_EXISTING_ARITHMETIC_MISMATCH")
        feature = insert_economic_feature(
            session,
            event_key=event_key,
            generated_at=utc_now(),
            category="cpi",
            surprise_score=calculated["surprise_score"],
            direction=calculated["direction"],
            confidence_score=calculated["confidence_score"],
            raw_json=dict(
                calculated, meaning=inputs.momentum_status, source_sha256=inputs.source_sha256
            ),
        )
        link = insert_economic_market_link(
            session,
            ticker=ticker,
            event_key=event_key,
            category="cpi",
            confidence=100,
            reason="EXPLICIT_FROZEN_LAGGED_PREDICTOR_MAPPING_NOT_SETTLEMENT",
            raw_json=mapping,
        )
        if session.get(Market, ticker) is not None:
            raise ValueError("ECONOMIC_EXISTING_MARKET_AMBIGUOUS")
        session.add(
            Market(
                ticker=ticker,
                event_ticker=m["event_ticker"],
                series_ticker="KXCPI",
                title=m.get("title"),
                status=m["status"],
                raw_json=json.dumps(m),
                first_seen_at=started,
                last_seen_at=started,
            )
        )
        session.flush()
        snapshot = MarketSnapshot(
            ticker=ticker,
            captured_at=aware(originals[2]["received_at"]),
            status=m["status"],
            best_yes_bid=str(bid),
            best_yes_ask=str(ask),
            raw_market_json=json.dumps(m),
            raw_orderbook_json=json.dumps(originals[2]["body"]),
        )
        session.add(snapshot)
        session.flush()
        output = EconomicV1Forecaster().forecast(session, snapshot)
        completed = utc_now()
        if output is None or output.feature_json["economic_feature_id"] != feature.id:
            raise ValueError("ECONOMIC_ACTUAL_FORECAST_REQUIRED")
        if (
            not baseline_at
            <= aware(output.feature_json["input_cutoff"])
            <= output.forecasted_at
            <= completed
        ):
            raise ValueError("ECONOMIC_ACTUAL_FORECAST_CLOCK_INVALID")
        after = _frozen(frozen_model, frozen_procedure, model_code, settings, root, completed)
        finished = utc_now()
        if before != after or not completed <= finished < min(release, aware(m["close_time"])):
            raise ValueError("ECONOMIC_EXECUTION_CHANGED_OR_CLOSED")
        if any((finished - aware(x["received_at"])).total_seconds() > 60 for x in originals):
            raise ValueError("ECONOMIC_BOOK_AGED_DURING_EXECUTION")
        if (
            build_economic_source_features(source, decision_at=finished).observations
            != inputs.observations
        ):
            raise ValueError("ECONOMIC_SOURCE_CHANGED_DURING_EXECUTION")
        original = _json(asdict(output))
        return _artifact(
            dict(
                kind="lagged-cpi-execution-v1",
                procedure_sha256=frozen_procedure.sha256,
                model_artifact_sha256=frozen_model.sha256,
                model_code_sha256=hashlib.sha256(model_code).hexdigest(),
                code_dependencies=before,
                dependencies_before=before,
                dependencies_after=after,
                settings_original=settings.model_dump(mode="json"),
                settings_sha256=canonical_hash(settings.model_dump(mode="json")),
                source_original_sha256=source.original.sha256,
                source_url=source.original.url,
                source_received_at=source.original.received_at,
                source_features=asdict(inputs),
                market_envelope_sha256=market.sha256,
                event_envelope_sha256=event.sha256,
                series_envelope_sha256=series.sha256,
                book_envelope_sha256=book.sha256,
                original_book_sha256=canonical_hash(originals[2]["body"]),
                ticker=ticker,
                event_ticker=mapping["event_ticker"],
                contract_mapping=mapping,
                ephemeral_snapshot_id=snapshot.id,
                ephemeral_feature_id=feature.id,
                ephemeral_link_id=link.id,
                feature_original=calculated,
                feature_record_original=dict(
                    id=feature.id,
                    event_key=feature.event_key,
                    generated_at=feature.generated_at,
                    created_at=feature.created_at,
                    category=feature.category,
                    surprise_score=feature.surprise_score,
                    direction=feature.direction,
                    confidence_score=feature.confidence_score,
                    raw_json=feature.raw_json,
                ),
                link_original=dict(
                    id=link.id,
                    ticker=link.ticker,
                    event_key=link.event_key,
                    detected_at=link.detected_at,
                    category=link.category,
                    confidence=link.confidence,
                    reason=link.reason,
                    raw_json=link.raw_json,
                ),
                forecast_original=original,
                forecast_sha256=canonical_hash(original),
                source_off_probability=str(midpoint),
                source_on_probability=str(output.yes_probability),
                source_off_generated_at=baseline_at,
                execution_started_at=started,
                forecast_generated_at=output.forecasted_at,
                forecast_available_at=completed,
                execution_finished_at=finished,
                receipt_generated_at=utc_now(),
                variant_methods=METHODS,
                contrast_type=CONTRAST,
                verification_scope="FILESYSTEM_BUNDLE_AND_IMPORTED_ORIGINS_BEFORE_AFTER_EXECUTION",
                atomic_filesystem_immutability=False,
                ephemeral_session=True,
                research_only=True,
                runtime_certified=False,
                settlement_eligible=False,
                fee_execution_verified=False,
            )
        )
