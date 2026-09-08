"""Durable existing-stack weather preparation, separate from guarded admission."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import Settings
from kalshi_predictor.overnight_paper import preparation
from kalshi_predictor.overnight_paper.coordinator import (
    _owned_session_factory,
    _verify_database,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.qualification import EvidenceReference
from kalshi_predictor.overnight_paper.runtime_owner import (
    RuntimeOwner,
    acquire_runtime_owner,
    validate_runtime_owner,
)
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.overnight_paper.store import digest, encode
from kalshi_predictor.utils.time import utc_now


def _journal_value(value: Any) -> Any:
    """Explicit lossless data conversion; never stringify unknown executable objects."""
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_JOURNAL_DECIMAL")
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("JOURNAL_TIMEZONE_REQUIRED")
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JOURNAL_STRING_KEYS_REQUIRED")
        return {key: _journal_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_journal_value(item) for item in value]
    raise ValueError("UNSUPPORTED_JOURNAL_VALUE")


@dataclass(frozen=True)
class PreparationCycle:
    record: dict[str, Any]
    live_result: preparation.WeatherPreparationResult | None


@dataclass(frozen=True)
class FrozenWeatherExecution:
    """Original precommitted research procedure and existing model definition."""

    procedure: Artifact
    model: Artifact
    model_code: bytes
    repository: Path


_WEATHER_ENTRYPOINT = "kalshi_predictor.forecasting.weather_v2:WeatherV2Forecaster.forecast"
_VARIANTS = {"off": "same_book_midpoint", "on": _WEATHER_ENTRYPOINT}


def _verify_frozen_execution(
    frozen: FrozenWeatherExecution,
    settings: Settings,
    at: datetime,
) -> dict[str, str]:
    # Local import avoids adding an initialization cycle to existing preparation.
    from kalshi_predictor.overnight_paper.candidate_assembly import weather_model_code_bundle

    if type(frozen) is not FrozenWeatherExecution:
        raise ValueError("FROZEN_WEATHER_EXECUTION_REQUIRED")
    procedure, model = frozen.procedure.decode(), frozen.model.decode()
    config = settings.model_dump(mode="json")
    if (
        procedure.get("kind") != "weather-paired-procedure-v1"
        or procedure.get("variant_methods") != _VARIANTS
        or procedure.get("contrast_type") != "MARKET_BASELINE_VS_WEATHER_V2"
        or procedure.get("source_id") != "NWS"
        or procedure.get("model_artifact_sha256") != frozen.model.sha256
        or procedure.get("settings_sha256") != canonical_hash(config)
        or settings.weather_v2_knyc_observation_enabled
        or model.get("name") != "weather_v2"
        or model.get("model_kind") != "fixed_heuristic"
        or model.get("training_cutoff") is not None
        or model.get("training_dataset_hashes") != []
        or model.get("parameters") != config
        or model.get("parameters_sha256") != canonical_hash(config)
        or model.get("model_entrypoint") != _WEATHER_ENTRYPOINT
        or model.get("code_sha256") != hashlib.sha256(frozen.model_code).hexdigest()
    ):
        raise ValueError("FROZEN_WEATHER_PROCEDURE_OR_MODEL_MISMATCH")
    for item in (model, procedure):
        if (
            not isinstance(item.get("name"), str)
            or not item["name"].strip()
            or not isinstance(item.get("version"), str)
            or not item["version"].strip()
            or not aware(item["created_at"])
            <= aware(item["frozen_at"])
            <= aware(item["available_at"])
            <= at
        ):
            raise ValueError("FROZEN_WEATHER_VISIBILITY_INVALID")
    if aware(model["available_at"]) > aware(procedure["frozen_at"]):
        raise ValueError("MODEL_NOT_AVAILABLE_AT_PROCEDURE_FREEZE")
    dependencies, current_code = weather_model_code_bundle(frozen.repository)
    if current_code != frozen.model_code or dependencies != model.get("code_dependencies"):
        raise ValueError("FROZEN_WEATHER_CODE_CHANGED")
    # Check actual loaded module origins; this is not atomic filesystem locking
    # or an assertion that all possible runtime monkeypatches were excluded.
    bundle = json.loads(current_code)
    root = frozen.repository.resolve(strict=True)
    for source in bundle["sources"]:
        module = sys.modules.get(source["module"])
        if module is not None:
            origin = getattr(module, "__file__", None)
            if origin is None or Path(origin).resolve(strict=True) != (
                root / source["path"]
            ).resolve(strict=True):
                raise ValueError("FROZEN_WEATHER_IMPORTED_ORIGIN_MISMATCH")
    module = sys.modules.get("kalshi_predictor.forecasting.weather_v2")
    if module is None or preparation.WeatherV2Forecaster is not getattr(
        module, "WeatherV2Forecaster", None
    ):
        raise ValueError("FROZEN_WEATHER_ENTRYPOINT_IMPORT_MISMATCH")
    return dependencies


def _execution_receipt(
    frozen: FrozenWeatherExecution,
    result: preparation.WeatherPreparationResult,
    settings: Settings,
    started: datetime,
    finished: datetime,
    dependencies: dict[str, str],
) -> dict[str, Any] | None:
    if result.forecast_output is None:
        return None
    records = result.records
    forecast = _journal_value(asdict(result.forecast_output))
    if forecast != _journal_value(records["forecast"]):
        raise ValueError("FROZEN_WEATHER_FORECAST_ORIGINAL_MISMATCH")
    generated, available = (
        aware(records["forecast_generated_at"]),
        aware(records["forecast_available_at"]),
    )
    if (
        not started <= generated <= available <= finished
        or aware(forecast["forecasted_at"]) != generated
    ):
        raise ValueError("FROZEN_WEATHER_EXECUTION_CLOCK_INVALID")
    originals = [json.loads(s.payload) for s in result.source_envelopes]
    book_url = f"https://external-api.kalshi.com/trade-api/v2/markets/{result.ticker}/orderbook"
    books = [s for s in originals if s["url"] == book_url]
    if len(books) != 1 or books[0]["body"] != records["book"]:
        raise ValueError("FROZEN_WEATHER_BOOK_ORIGINAL_MISMATCH")
    yes = records["book_qualification"]["sides"]["YES"]
    bid, ask = Decimal(str(yes["bid"])), Decimal(str(yes["ask"]))
    midpoint = (bid + ask) / 2
    baseline_generated = utc_now()
    if not 0 <= bid <= ask <= 1 or midpoint != Decimal(forecast["market_mid_probability"]):
        raise ValueError("FROZEN_WEATHER_MIDPOINT_MISMATCH")
    return dict(
        kind="weather-preparation-execution-v1",
        procedure_sha256=frozen.procedure.sha256,
        model_artifact_sha256=frozen.model.sha256,
        model_code_sha256=hashlib.sha256(frozen.model_code).hexdigest(),
        settings_sha256=canonical_hash(settings.model_dump(mode="json")),
        code_dependencies=dependencies,
        dependencies_before=dependencies,
        dependencies_after=dependencies,
        settings_original=settings.model_dump(mode="json"),
        variant_methods=_VARIANTS,
        contrast_type="MARKET_BASELINE_VS_WEATHER_V2",
        source_envelope_hashes=[s.sha256 for s in result.source_envelopes],
        snapshot_id=records["snapshot_id"],
        ticker=result.ticker,
        original_book_sha256=canonical_hash(books[0]["body"]),
        book_envelope_sha256=next(
            s.sha256 for s in result.source_envelopes if json.loads(s.payload)["url"] == book_url
        ),
        source_off_probability=str(midpoint),
        source_off_generated_at=baseline_generated.isoformat(),
        source_on_probability=forecast["yes_probability"],
        forecast_original=forecast,
        forecast_sha256=canonical_hash(forecast),
        preparation_started_at=started.isoformat(),
        preparation_finished_at=finished.isoformat(),
        forecast_generated_at=generated.isoformat(),
        forecast_available_at=available.isoformat(),
        receipt_generated_at=utc_now().isoformat(),
        verification_scope="FILESYSTEM_BUNDLE_AND_IMPORTED_ORIGINS_BEFORE_AFTER_PREPARATION",
        atomic_filesystem_immutability=False,
        research_only=True,
        runtime_certified=False,
    )


def _run_weather_preparation_live_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    frozen_execution: FrozenWeatherExecution | None = None,
) -> PreparationCycle:
    """Compute once and preserve rejection or computation on the same ledger.

    Replay returns the original historical computation and does not refresh its
    clocks. A changed request needs a new cycle ID. This never admits a trade.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", cycle_id):
        raise ValueError("INVALID_PREPARATION_CYCLE_ID")
    assert_public_only_settings(settings)
    if not 1 <= len(source_envelopes) <= 12 or any(
        len(source.payload) > 1_000_000 or not source.valid() for source in source_envelopes
    ):
        raise ValueError("BOUNDED_ORIGINAL_ENVELOPES_REQUIRED")
    path = database_path.resolve(strict=True)
    if "onedrive" in str(path).lower() or any(
        item.is_symlink() or getattr(item, "is_junction", lambda: False)()
        for item in (database_path, *database_path.parents)
    ):
        raise ValueError("ISOLATED_UNLINKED_DATABASE_REQUIRED")
    factory = _owned_session_factory(session_factory, path)
    request = {
        "ticker": ticker,
        "source_hashes": [source.sha256 for source in source_envelopes],
        "settings": settings.model_dump(mode="json"),
        "slippage_allowance": str(slippage_allowance),
        "uncertainty_buffer": str(uncertainty_buffer),
        "preparation_source_sha256": hashlib.sha256(
            Path(preparation.__file__).read_bytes()
        ).hexdigest(),
    }
    if frozen_execution is not None:
        request["frozen_execution"] = {
            "procedure_sha256": frozen_execution.procedure.sha256,
            "model_sha256": frozen_execution.model.sha256,
            "model_code_sha256": hashlib.sha256(frozen_execution.model_code).hexdigest(),
        }
    request_id = digest(request)
    key = "weather-preparation:" + cycle_id
    with factory() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        _verify_database(session, path)
        stored = session.execute(
            text("SELECT payload FROM overnight_sprint_cycles WHERE id=:id"), {"id": key}
        ).scalar_one_or_none()
        if stored is not None:
            record = json.loads(stored)
            if record.get("request_id") != request_id or record.get("request") != request:
                raise ValueError("PREPARATION_CYCLE_INPUT_CONFLICT")
            session.rollback()
            return PreparationCycle(record, None)
        started = utc_now()
        dependencies = None
        if frozen_execution is not None:
            dependencies = _verify_frozen_execution(frozen_execution, settings, started)
        execution_started = utc_now()
        result = preparation.prepare_weather_candidate(
            session,
            ticker=ticker,
            source_envelopes=source_envelopes,
            settings=settings,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
        )
        execution_finished = utc_now()
        execution_receipt = None
        if frozen_execution is not None:
            after = _verify_frozen_execution(frozen_execution, settings, utc_now())
            if dependencies != after:
                raise ValueError("FROZEN_WEATHER_DEPENDENCIES_CHANGED_DURING_PREPARATION")
            execution_receipt = _execution_receipt(
                frozen_execution,
                result,
                settings,
                execution_started,
                execution_finished,
                after,
            )
        record = _journal_value(
            {
                "kind": "PAPER_RELEASE_PREPARATION",
                "request_id": request_id,
                "request": request,
                "started_at": started,
                "finished_at": utc_now(),
                "state": result.state,
                "blockers": result.blockers,
                "records": result.records,
                "decision": None if result.decision is None else asdict(result.decision),
                "original_sources": [
                    {
                        "artifact": source.artifact,
                        "sha256": source.sha256,
                        "original_utf8": source.payload.decode("utf-8"),
                    }
                    for source in source_envelopes
                ],
                "orders_created": 0,
                "current_eligibility": False,
            }
        )
        if frozen_execution is not None:
            record["frozen_procedure_original"] = frozen_execution.procedure.decode()
            record["frozen_model_original"] = frozen_execution.model.decode()
            record["execution_receipt"] = execution_receipt
            record["execution_receipt_sha256"] = (
                None if execution_receipt is None else canonical_hash(execution_receipt)
            )
        session.execute(
            text("INSERT INTO overnight_sprint_cycles(id,captured_at,payload) VALUES(:id,:at,:p)"),
            {"id": key, "at": record["finished_at"], "p": encode(record)},
        )
        session.commit()
        return PreparationCycle(record, result)


def run_weather_preparation_live_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    runtime_owner: RuntimeOwner | None = None,
    frozen_execution: FrozenWeatherExecution | None = None,
) -> PreparationCycle:
    """Own the ledger or validate an already-held same-process driver owner."""
    assert_public_only_settings(settings)
    if runtime_owner is not None:
        validate_runtime_owner(runtime_owner, database_path)
    ownership = (
        acquire_runtime_owner(database_path)
        if runtime_owner is None
        else nullcontext(runtime_owner)
    )
    with ownership as owner:
        validate_runtime_owner(owner, database_path)
        return _run_weather_preparation_live_cycle(
            session_factory=session_factory,
            database_path=database_path,
            cycle_id=cycle_id,
            ticker=ticker,
            source_envelopes=source_envelopes,
            settings=settings,
            slippage_allowance=slippage_allowance,
            uncertainty_buffer=uncertainty_buffer,
            frozen_execution=frozen_execution,
        )


def run_weather_preparation_cycle(
    *,
    session_factory: sessionmaker[Session],
    database_path: Path,
    cycle_id: str,
    ticker: str,
    source_envelopes: tuple[EvidenceReference, ...],
    settings: Settings,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
    runtime_owner: RuntimeOwner | None = None,
    frozen_execution: FrozenWeatherExecution | None = None,
) -> dict[str, Any]:
    """Historical journal API; replay never reconstructs current engine objects."""
    return run_weather_preparation_live_cycle(
        session_factory=session_factory,
        database_path=database_path,
        cycle_id=cycle_id,
        ticker=ticker,
        source_envelopes=source_envelopes,
        settings=settings,
        slippage_allowance=slippage_allowance,
        uncertainty_buffer=uncertainty_buffer,
        runtime_owner=runtime_owner,
        frozen_execution=frozen_execution,
    ).record
