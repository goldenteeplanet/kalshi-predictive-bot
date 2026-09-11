"""Fixed book capture with optional supplied independent crypto price history.

An isolated unqualified research protocol; the market ensemble stays market-only.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from kalshi_predictor.active_universe import is_active_market_status
from kalshi_predictor.config import Settings
from kalshi_predictor.crypto.research_provenance import (
    freeze_code,
    freeze_prediction,
    verify_unchanged,
)
from kalshi_predictor.crypto.shared_capture import SharedCryptoInputs
from kalshi_predictor.data.repositories import insert_forecast, insert_market_snapshot
from kalshi_predictor.data.schema import (
    Base,
    Forecast,
    MarketSnapshot,
    MicrostructureFeature,
    ModelWeight,
)
from kalshi_predictor.forecasting.crypto_v3_independent import forecast_independent
from kalshi_predictor.forecasting.ensemble_v2 import EnsembleV2Forecaster
from kalshi_predictor.forecasting.microstructure_v1 import MicrostructureV1Forecaster
from kalshi_predictor.forecasting.registry_helpers import MarketImpliedSnapshotForecaster
from kalshi_predictor.microstructure.orderbook_features import compute_microstructure_feature
from kalshi_predictor.microstructure.provenance import (
    FeatureBuildContext,
    RecordReceipt,
    aware,
    bound_close_time,
    record_hash,
    record_time,
    settings_hash,
)
from kalshi_predictor.microstructure.repository import insert_microstructure_feature

BASE = "https://api.elections.kalshi.com/trade-api/v2/markets/"
MAX_BYTES = 1_000_000
MAX_SECONDS = 90.0
SOURCE_PATHS = tuple(
    "src/kalshi_predictor/" + name
    for name in (
        "microstructure/research_capture.py",
        "microstructure/provenance.py",
        "microstructure/orderbook_features.py",
        "microstructure/repository.py",
        "microstructure/imbalance.py",
        "microstructure/smart_money.py",
        "microstructure/late_moves.py",
        "microstructure/dislocation.py",
        "microstructure/liquidity_tracker.py",
        "microstructure/spread_tracker.py",
        "microstructure/signals.py",
        "forecasting/microstructure_v1.py",
        "forecasting/ensemble_v2.py",
        "forecasting/registry_helpers.py",
        "forecasting/market_implied.py",
        "forecasting/base.py",
        "kalshi/orderbook.py",
        "data/repositories.py",
        "data/schema.py",
        "data/db.py",
        "config.py",
        "active_universe.py",
        "utils/time.py",
        "utils/decimals.py",
        "tournament/repository.py",
        "tournament/ranking.py",
        "memory/capture.py",
        "memory/contracts.py",
        "memory/repository.py",
        "provenance/dual_write.py",
        "crypto/research_provenance.py",
    )
) + ("scripts/positive_ev_microstructure_research.py",)


def now() -> datetime:
    return datetime.now(UTC)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()


def write_new(path: Path, raw: bytes) -> str:
    if len(raw) > 8_000_000:
        raise ValueError("ARTIFACT_SIZE_CAP")
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return digest(raw)


def load(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("NONFINITE_JSON")

    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(value, dict):
        raise ValueError("JSON_OBJECT_REQUIRED")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def acquire(url: str) -> tuple[bytes, int]:
    # Constructed fixed-origin public URLs only; no account/client credentials.
    with build_opener(NoRedirect()).open(
        Request(url, headers={"User-Agent": "bounded-research/1"}), timeout=10
    ) as response:
        return response.read(MAX_BYTES + 1), response.status


def record_payload(record: MarketSnapshot | Forecast | MicrostructureFeature) -> dict:
    return {
        column.name: record_time(value).isoformat()
        if isinstance(value := getattr(record, column.name), datetime)
        else value
        for column in record.__table__.columns
    }


def runtime_sources(repo: Path) -> tuple[str, ...]:
    paths = set(SOURCE_PATHS)
    source_root = (repo / "src").resolve()
    for name, module in list(sys.modules.items()):
        if not name.startswith("kalshi_predictor"):
            continue
        filename = getattr(module, "__file__", None)
        if filename is None:
            continue
        path = Path(filename).resolve()
        if not path.is_relative_to(source_root) or path.suffix != ".py":
            raise ValueError("IMPORTED_SOURCE_OUTSIDE_REPOSITORY")
        expected = source_root.joinpath(*name.split("."))
        if path not in {expected.with_suffix(".py"), expected / "__init__.py"}:
            raise ValueError("IMPORTED_MODULE_IDENTITY_MISMATCH")
        paths.add(path.relative_to(repo.resolve()).as_posix())
    if len(paths) > 256 or sum((repo / name).stat().st_size for name in paths) > 8_000_000:
        raise ValueError("SOURCE_CLOSURE_SIZE_CAP")
    return tuple(sorted(paths))


def run(
    repo: Path,
    output: Path,
    ticker: str,
    *,
    settings: Settings,
    crypto_inputs: SharedCryptoInputs | None = None,
    get: Callable[[str], tuple[bytes, int]] = acquire,
    clock: Callable[[], datetime] = now,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,127}", ticker):
        raise ValueError("EXACT_PUBLIC_TICKER_REQUIRED")
    if settings.microstructure_min_snapshots != 3:
        raise ValueError("FIXED_THREE_SAMPLE_PROTOCOL_REQUIRES_CONFIGURED_QUORUM_THREE")
    if settings.microstructure_lookback_minutes < 1:
        raise ValueError("POSITIVE_LOOKBACK_REQUIRED")
    if crypto_inputs is not None:
        crypto_inputs.prices(aware(clock()))  # Fail before output creation or acquisition.
    settings = settings.model_copy(deep=True)
    output.mkdir(parents=True, exist_ok=False)
    # freeze_code rejects dirty dependencies. No request before reviewed/committed source.
    for relative in SOURCE_PATHS:
        if relative.startswith("src/"):
            importlib.import_module(relative[4:-3].replace("/", "."))
    proof = freeze_code(repo, output / "code", runtime_sources(repo))
    verify_unchanged(repo, proof)
    write_new(output / "code-proof.json", encoded(proof))
    effective = {
        k: str(v) for k, v in settings.model_dump().items() if k.startswith("microstructure_")
    }
    write_new(
        output / "settings.json",
        encoded({"effective": effective, "settings_sha256": settings_hash(settings)}),
    )
    protocol = {
        "schema": "isolated-microstructure-research-v1",
        "scope": "MARKET_ONLY_ENSEMBLE_RESEARCH",
        "ticker": ticker,
        "samples": 3,
        "interval_seconds": 5,
        "max_gets": 6,
        "max_seconds": MAX_SECONDS,
        "deadline_enforcement": "CHECKED_BETWEEN_OPERATIONS_NOT_HARD_CANCELLATION",
        "network_timeout_seconds": 10,
        "max_response_bytes": MAX_BYTES,
        "provider_observed_at": None,
        "snapshot_clock_semantics": "LOCAL_CAPTURE_TIME_NOT_PROVIDER_OBSERVATION",
        "max_total_output_bytes": 64_000_000,
        "isolated_weights": "EMPTY_BY_PROTOCOL_NOT_PRODUCTION_ABSENCE",
        "component_models": ["market_implied_v1"],
        "independent_alpha": False,
        "independent_forecast_present": crypto_inputs is not None,
        "production_replay": False,
        "release_certified": False,
        "execution_authority": False,
    }
    if crypto_inputs is not None:
        protocol["scope"] = "SHARED_CUTOFF_CRYPTO_AND_MARKET_RESEARCH"
    write_new(output / "protocol.json", encoded(protocol))
    artifact_hashes = {
        name: digest((output / name).read_bytes())
        for name in ("protocol.json", "settings.json", "code-proof.json")
    }
    if crypto_inputs is not None:
        from dataclasses import asdict

        # Separate extension declares supplied candle provenance and unchanged ensemble.
        extension = {
            "schema": "shared-crypto-extension-v1",
            "target": asdict(crypto_inputs.target),
            "scope": protocol["scope"],
            "same_input_cutoff": True,
            "ensemble_components_unchanged": ["market_implied_v1"],
            "settlement_alignment": "TERMINAL_PRICE_PROXY_UNVERIFIED",
            "v2_status": "UNAVAILABLE_COMPATIBLE_CF_FEATURES_AND_LINK_LINEAGE",
        }
        artifact_hashes["crypto-extension.json"] = write_new(
            output / "crypto-extension.json", encoded(extension)
        )
        for index, original in enumerate(crypto_inputs.originals):
            name = f"crypto-candles-{index}.json"
            artifact_hashes[name] = write_new(output / name, original.raw)
            name += ".receipt.json"
            artifact_hashes[name] = write_new(
                output / name,
                encoded(
                    {
                        "url": original.url,
                        "sha256": original.sha256,
                        "status": original.status,
                        "received_at": original.received_at,
                        "archived_at": aware(clock()),
                        "clock_authority": "SUPPLIED_LOCAL_RECEIPT",
                    }
                ),
            )
    last_clock = aware(clock())
    started = monotonic()
    target: datetime | None = None
    requests = 0
    receipts: list[dict] = []
    record_manifests: list[tuple[str, dict]] = []

    def check() -> datetime:
        nonlocal last_clock
        current = aware(clock())
        elapsed = monotonic() - started
        if current < last_clock or elapsed < 0 or elapsed > MAX_SECONDS:
            raise ValueError("CLOCK_REGRESSION_OR_TIME_CAP")
        if target is not None and current >= target:
            raise ValueError("TARGET_EXPIRED")
        last_clock = current
        return current

    def fetch(url: str, name: str) -> tuple[dict, dict]:
        nonlocal requests
        check()
        if requests >= 6:
            raise ValueError("SIX_GET_CAP")
        requests += 1
        raw, status = get(url)
        received = check()
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES or status != 200:
            raise ValueError("PUBLIC_RESPONSE_STATUS_OR_SIZE")
        sha = write_new(output / name, raw)
        recorded = check()
        receipt = {
            "url": url,
            "status": status,
            "sha256": sha,
            "received_at": received.isoformat(),
            "recorded_at": recorded.isoformat(),
            "path": name,
            "provider_observed_at": None,
            "observation_clock_status": "UNKNOWN_PROVIDER_TIMESTAMP",
        }
        write_new(output / (name + ".receipt.json"), encoded(receipt))
        receipts.append(receipt)
        return load(raw), receipt

    def freeze_record(record, name, dependencies, computed_at):
        session.commit()  # genuine generated IDs, persisted only in this isolated file
        payload = record_payload(record)
        sha = record_hash(record)
        if digest(json.dumps(payload, sort_keys=True, allow_nan=False).encode()) != sha:
            raise ValueError("RECORD_SERIALIZATION_HASH_MISMATCH")
        file_sha = write_new(output / (name + ".json"), encoded(payload))
        recorded = check()
        manifest = {
            "record_id": record.id,
            "record_sha256": sha,
            "file_sha256": file_sha,
            "recorded_at": recorded.isoformat(),
            "dependencies": dependencies,
            "computed_at": aware(computed_at).isoformat(),
            "forecasted_at_semantics": "SNAPSHOT_REFERENCE_NOT_COMPUTATION_OR_AVAILABILITY",
        }
        if not aware(computed_at) <= recorded:
            raise ValueError("RECORD_COMPLETION_CLOCK_MISMATCH")
        write_new(output / (name + ".receipt.json"), encoded(manifest))
        record_manifests.append((name, manifest))
        return RecordReceipt(record.id, sha, recorded)

    engine = None
    try:
        check()
        engine = create_engine("sqlite:///" + str((output / "research.sqlite").resolve()))
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA max_page_count=4096")  # 16MiB with default4KiB pages
            Base.metadata.create_all(connection)
        with Session(engine, expire_on_commit=False) as session:
            snapshots = []
            snapshot_receipts = []
            last_sample = None
            fixed_metadata = None
            for index in range(3):
                if last_sample is not None:
                    sleep(5)
                    if (check() - last_sample).total_seconds() < 5:
                        raise ValueError("SAMPLING_INTERVAL_NOT_ELAPSED")
                market_payload, market_receipt = fetch(BASE + ticker, f"sample-{index}-market.json")
                market = market_payload.get("market")
                if (
                    not isinstance(market, dict)
                    or market.get("ticker") != ticker
                    or not is_active_market_status(market.get("status"))
                ):
                    raise ValueError("ACTIVE_EXACT_MARKET_REQUIRED")
                metadata = {
                    key: market.get(key)
                    for key in (
                        "ticker",
                        "event_ticker",
                        "series_ticker",
                        "title",
                        "subtitle",
                        "strike_type",
                        "custom_strike",
                        "floor_strike",
                        "cap_strike",
                        "rules_primary",
                        "rules_secondary",
                    )
                }
                if fixed_metadata is not None and metadata != fixed_metadata:
                    raise ValueError("CONTRACT_METADATA_CHANGED")
                fixed_metadata = metadata
                if crypto_inputs is not None:
                    crypto_inputs.bind_market(
                        market, cutoff=aware(datetime.fromisoformat(market_receipt["received_at"]))
                    )
                close = bound_close_time(market)
                if target is not None and close != target:
                    raise ValueError("TARGET_METADATA_CHANGED")
                target = close
                check()
                book, book_receipt = fetch(
                    BASE + ticker + "/orderbook?depth=5", f"sample-{index}-book.json"
                )
                container = book.get("orderbook_fp")
                if not isinstance(container, dict):
                    raise ValueError("FIXED_POINT_BOOK_REQUIRED")
                for side in ("yes_dollars", "no_dollars"):
                    levels = container.get(side)
                    if not isinstance(levels, list) or not levels or len(levels) > 5:
                        raise ValueError("BOUNDED_TWO_SIDED_DEPTH_REQUIRED")
                    for level in levels:
                        if (
                            not isinstance(level, list)
                            or len(level) != 2
                            or any(isinstance(x, bool) for x in level)
                        ):
                            raise ValueError("INVALID_DEPTH_LEVEL")
                        price, quantity = map(Decimal, level)
                        if (
                            not price.is_finite()
                            or not quantity.is_finite()
                            or not 0 < price < 1
                            or quantity <= 0
                        ):
                            raise ValueError("INVALID_DEPTH_VALUE")
                if (
                    max(Decimal(x[0]) for x in container["yes_dollars"])
                    + max(Decimal(x[0]) for x in container["no_dollars"])
                    > 1
                ):
                    raise ValueError("CROSSED_ORDERBOOK")
                captured = check()
                snapshot = insert_market_snapshot(session, market, book, captured)
                receipt = freeze_record(
                    snapshot, f"snapshot-{index}", [market_receipt, book_receipt], captured
                )
                snapshots.append(snapshot)
                snapshot_receipts.append(receipt)
                last_sample = captured
            check()
            component = MarketImpliedSnapshotForecaster().forecast(session, snapshots[-1])
            if component is None:
                raise ValueError("ACTUAL_COMPONENT_UNAVAILABLE")
            component_computed = check()
            component_record = insert_forecast(
                session, component, market_snapshot_id=snapshots[-1].id, attribution_enabled=False
            )
            component_receipt = freeze_record(
                component_record,
                "component",
                {
                    "snapshot_id": snapshots[-1].id,
                    "snapshot_sha256": snapshot_receipts[-1].record_sha256,
                },
                component_computed,
            )
            if list(session.scalars(select(ModelWeight))):
                raise ValueError("ISOLATED_WEIGHTS_NOT_EMPTY")
            if list(session.scalars(select(Forecast.model_name))) != ["market_implied_v1"]:
                raise ValueError("ISOLATED_COMPONENT_UNIVERSE_CHANGED")
            weights_sha = write_new(
                output / "weights.json",
                encoded({"rows": [], "scope": "ISOLATED_PROTOCOL_NOT_PRODUCTION_ABSENCE"}),
            )
            artifact_hashes["weights.json"] = weights_sha
            ensemble = EnsembleV2Forecaster().forecast(session, snapshots[-1])
            if ensemble is None or set(ensemble.feature_json["component_forecasts"]) != {
                "market_implied_v1"
            }:
                raise ValueError("ACTUAL_ENSEMBLE_UNAVAILABLE")
            ensemble_computed = check()
            ensemble_record = insert_forecast(
                session, ensemble, market_snapshot_id=snapshots[-1].id, attribution_enabled=False
            )
            ensemble_receipt = freeze_record(
                ensemble_record,
                "ensemble",
                {
                    "component_sha256": component_receipt.record_sha256,
                    "weights_sha256": weights_sha,
                },
                ensemble_computed,
            )
            cutoff = check()
            context = FeatureBuildContext(
                cutoff, cutoff, tuple(snapshot_receipts), ensemble_receipt
            )
            values = compute_microstructure_feature(
                session,
                snapshots,
                lookback_minutes=settings.microstructure_lookback_minutes,
                settings=settings,
                context=context,
            )
            feature = insert_microstructure_feature(session, values)
            feature_receipt = freeze_record(
                feature,
                "feature",
                {
                    "snapshots": [r.record_sha256 for r in snapshot_receipts],
                    "ensemble": ensemble_receipt.record_sha256,
                },
                values["created_at"],
            )
            result = MicrostructureV1Forecaster(settings).forecast_bound(
                session,
                snapshots[-1],
                feature_id=feature.id,
                feature_sha256=feature_receipt.record_sha256,
                context=context,
            )
            if result is None:
                raise ValueError("STRICT_MICROSTRUCTURE_UNAVAILABLE")
            independent = None
            if crypto_inputs is not None:
                independent = forecast_independent(
                    crypto_inputs.prices(cutoff), crypto_inputs.target, decision_at=cutoff
                )
                independent = {
                    **independent,
                    "actual_computed_at": check().isoformat(),
                    "timing_status": "CUTOFF_REFERENCE_NOT_COMPUTATION_CLOCK",
                }
            check()
            verify_unchanged(repo, proof)
            if not set(runtime_sources(repo)).issubset({p["path"] for p in proof["files"]}):
                raise ValueError("UNFROZEN_RUNTIME_DEPENDENCY")
            for source in proof["files"]:
                if digest((output / "code" / source["path"]).read_bytes()) != source["sha256"]:
                    raise ValueError("ARCHIVED_SOURCE_HASH_MISMATCH")
            for name, sha in artifact_hashes.items():
                if digest((output / name).read_bytes()) != sha:
                    raise ValueError("PROTOCOL_ARTIFACT_CHANGED")
            for receipt in receipts:
                if load((output / (receipt["path"] + ".receipt.json")).read_bytes()) != receipt:
                    raise ValueError("SOURCE_RECEIPT_CHANGED")
                if digest((output / receipt["path"]).read_bytes()) != receipt["sha256"]:
                    raise ValueError("ORIGINAL_HASH_MISMATCH")
            for name, manifest in record_manifests:
                if digest((output / (name + ".json")).read_bytes()) != manifest["file_sha256"]:
                    raise ValueError("RECORDED_INPUT_HASH_MISMATCH")
                if load((output / (name + ".receipt.json")).read_bytes()) != manifest:
                    raise ValueError("RECORD_RECEIPT_CHANGED")
            if sum(p.stat().st_size for p in output.rglob("*") if p.is_file()) > 48_000_000:
                raise ValueError("TOTAL_OUTPUT_SIZE_CAP")
            prediction = {
                "scope": protocol["scope"],
                "ticker": ticker,
                "protocol": protocol,
                "protocol_artifact_hashes": artifact_hashes,
                "code_proof": proof,
                "settings_sha256": settings_hash(settings),
                "original_receipts": receipts,
                "record_manifests": dict(record_manifests),
                "component": record_payload(component_record),
                "ensemble": record_payload(ensemble_record),
                "microstructure": {
                    "yes_probability": str(result.yes_probability),
                    "feature_json": result.feature_json,
                },
                "shared_crypto": independent,
                "crypto_v2": {
                    "model_invoked": False,
                    "probability": None,
                    "status": "UNAVAILABLE_COMPATIBLE_CF_FEATURES_AND_LINK_LINEAGE",
                },
                "independent_alpha": False,
                "independent_forecast_present": crypto_inputs is not None,
                "release_certified": False,
                "execution_authority": False,
            }
            assert target is not None
            frozen = freeze_prediction(
                output / "frozen",
                prediction,
                model_input_as_of=cutoff,
                input_received_at=ensemble_receipt.received_at,
                model_committed_at=datetime.fromisoformat(proof["commit_recorded_at"]),
                target_at=target,
                clock=check,
            )
            report = {
                "status": "FROZEN_UNQUALIFIED",
                "scope": protocol["scope"],
                "ticker": ticker,
                "requests": requests,
                "target_at": target.isoformat(),
                "market_implied_probability": str(component.yes_probability),
                "ensemble_probability": str(ensemble.yes_probability),
                "microstructure_probability": str(result.yes_probability),
                "prediction_sha256": frozen["prediction_sha256"],
                "decision_at": frozen["decision_at"],
                "independent_alpha": False,
                "independent_forecast_present": crypto_inputs is not None,
                "production_replay": False,
                "certified_tournament_n": 0,
                "execution_authority": False,
            }
            write_new(output / "report.json", encoded(report))
            return report
    except Exception as exc:
        write_new(
            output / "failure.json",
            encoded(
                {
                    "status": "UNAVAILABLE",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                    "requests": requests,
                    "execution_authority": False,
                }
            ),
        )
        raise
    finally:
        if engine is not None:
            engine.dispose()
