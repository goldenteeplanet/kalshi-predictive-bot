"""Pure assembly from actual preparation outputs into the existing guarded path.

No transport or ledger mutation. Missing reviewed rules/model originals are
errors; computed qualification results never substitute for model evaluation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from .miami_development import DevelopmentRuleBinding

from kalshi_predictor.config import Settings
from kalshi_predictor.kalshi.orderbook import parse_orderbook
from kalshi_predictor.overnight_paper import rule_verifier
from kalshi_predictor.overnight_paper.boundary import (
    ExecutionMode,
    LocalPaperAuthorization,
    authorization_fingerprint,
)
from kalshi_predictor.overnight_paper.boundary_gate import audit_local_call_path
from kalshi_predictor.overnight_paper.coordinator import (
    PreparedCandidate,
    assert_public_only_settings,
)
from kalshi_predictor.overnight_paper.evaluation_dataset import build_observation
from kalshi_predictor.overnight_paper.gate_context import QualificationContext
from kalshi_predictor.overnight_paper.miami_preparation import (
    MiamiPreparationResult,
    verify_miami_preparation_handoff,
)
from kalshi_predictor.overnight_paper.miami_provenance import (
    BUNDLE_URL,
    MiamiBundleGateContext,
    miami_feature_record,
    verify_miami_provenance_source,
)
from kalshi_predictor.overnight_paper.miami_source_gate import VERIFIER as MIAMI_VERIFIER
from kalshi_predictor.overnight_paper.preparation import WeatherPreparationResult
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash
from kalshi_predictor.overnight_paper.provenance_gate import (
    ProvenanceContext,
    verify_complete_provenance,
)
from kalshi_predictor.overnight_paper.qualification import (
    COLLECTOR_GATES,
    PUBLIC_BASE,
    SEMANTIC_VERIFIERS,
    EvidenceReference,
    GateEvidence,
    compute_net_ev,
    qualify_candidate,
)
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.overnight_paper.source_health import aware
from kalshi_predictor.overnight_paper.timing import verify_settlement_horizon


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return aware(value).isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("NONFINITE_PREPARATION_VALUE")
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise ValueError("UNSERIALIZABLE_PREPARATION_VALUE")


def _artifact(value: dict[str, Any]) -> Artifact:
    raw = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return Artifact(hashlib.sha256(raw).hexdigest(), raw)


WEATHER_MODEL_ENTRYPOINT = "kalshi_predictor.forecasting.weather_v2:WeatherV2Forecaster.forecast"


def _model_code_bundle(repository: Path, *, miami: bool = False) -> tuple[dict[str, str], bytes]:
    """Read the conservative whole-class closure for an explicit model freeze.

    The auditor supports top-level classes, not method-qualified symbols. The
    entire forecaster class includes forecast and all its methods. Source hashes
    use the auditor's LF normalization; original UTF-8 text is retained in the
    canonical bundle. Calling this helper does not create or authorize a model.
    """
    repository = repository.resolve(strict=True)
    audit = audit_local_call_path(
        repository,
        entrypoints=(
            (
                "kalshi_predictor.weather.miami_half_hour_forecast",
                "forecast_miami_prior_day_grid30",
            ),
            ("kalshi_predictor.weather.miami_forecast", "empirical_probability"),
        )
        if miami
        else (("kalshi_predictor.forecasting.weather_v2", "WeatherV2Forecaster"),),
    )
    if not audit.passed:
        raise ValueError("WEATHER_MODEL_DEPENDENCY_AUDIT_FAILED:" + repr(audit.blockers))
    dependencies: dict[str, str] = {}
    sources = []
    for module, digest in audit.source_hashes:
        stem = repository / "src" / module.replace(".", "/")
        path = stem.with_suffix(".py")
        if not path.is_file():
            path = stem / "__init__.py"
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(repository / "src"):
            raise ValueError("MODEL_DEPENDENCY_PATH_OUTSIDE_REPOSITORY")
        raw = resolved.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("MODEL_DEPENDENCY_CHANGED_DURING_AUDIT")
        relative = path.relative_to(repository).as_posix()
        dependencies[relative] = digest
        sources.append(
            dict(module=module, path=relative, sha256=digest, source=raw.decode("utf-8"))
        )
    bundle = _artifact(
        dict(
            schema="miami-model-source-bundle-v1" if miami else "weather-model-source-bundle-v1",
            entrypoint=MIAMI_MODEL_ENTRYPOINT if miami else WEATHER_MODEL_ENTRYPOINT,
            sources=sources,
        )
    )
    return dependencies, bundle.payload


def weather_model_code_bundle(repository: Path) -> tuple[dict[str, str], bytes]:
    return _model_code_bundle(repository)


MIAMI_MODEL_ENTRYPOINT = (
    "kalshi_predictor.weather.miami_half_hour_forecast:forecast_miami_prior_day_grid30"
)


def miami_model_code_bundle(repository: Path) -> tuple[dict[str, str], bytes]:
    return _model_code_bundle(repository, miami=True)


def _verify_model_dependencies(
    model: dict[str, Any], model_code: bytes, repository: Path, *, miami: bool = False
) -> None:
    dependencies, current_bundle = _model_code_bundle(repository, miami=miami)
    expected_entrypoint = MIAMI_MODEL_ENTRYPOINT if miami else WEATHER_MODEL_ENTRYPOINT
    if model.get("model_entrypoint") != expected_entrypoint:
        raise ValueError("EXACT_WEATHER_MODEL_ENTRYPOINT_REQUIRED")
    if model.get("code_dependencies") != dependencies:
        raise ValueError("FROZEN_MODEL_DEPENDENCY_MISMATCH")
    if (
        model_code != current_bundle
        or model.get("code_sha256") != hashlib.sha256(model_code).hexdigest()
    ):
        raise ValueError("FROZEN_MODEL_ORIGINAL_CODE_BUNDLE_MISMATCH")


def _assemble_candidate(
    *,
    preparation: WeatherPreparationResult | MiamiPreparationResult,
    model: Artifact | None,
    model_code: bytes,
    settings: Settings,
    repository: Path,
    code_sha: str,
    authorization: LocalPaperAuthorization,
    rule_documents: tuple[RuleDocument, ...],
    now: datetime,
    include_evaluation_observation: bool = True,
    model_evaluation_head_sha256: str | None = None,
    miami_session: Session | None = None,
    _development_rule: DevelopmentRuleBinding | None = None,
) -> PreparedCandidate | Artifact:
    """Preserve preparation identities and construct verifiable original evidence.

    The fixed model must already be frozen against these exact credential-free
    Settings. Production registry authority is consulted directly, never passed
    as a candidate attestation. Runtime/code and every gate are rechecked by the
    existing qualification implementation; failed readiness remains diagnostic.
    """
    miami = type(preparation) is MiamiPreparationResult
    if isinstance(preparation, MiamiPreparationResult):
        if miami_session is None:
            raise ValueError("MIAMI_PERSISTED_SESSION_REQUIRED")
        verify_miami_preparation_handoff(miami_session, preparation, now=now)
    if (
        type(preparation) not in (WeatherPreparationResult, MiamiPreparationResult)
        or preparation.state != "COMPUTED_UNQUALIFIED"
    ):
        raise ValueError("COMPLETED_WEATHER_PREPARATION_REQUIRED")
    if model is None or not model_code:
        raise ValueError("FROZEN_ORIGINAL_MODEL_REQUIRED")
    if type(settings) is not Settings:
        raise ValueError("CONCRETE_SETTINGS_REQUIRED")
    config = settings.model_dump(mode="json")
    if any(
        config.get(key)
        for key in (
            "kalshi_api_key_id",
            "kalshi_private_key_path",
            "postgres_password",
            "execution_confirmation_token",
        )
    ):
        raise ValueError("CREDENTIAL_FREE_LOCAL_SETTINGS_REQUIRED")
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
    if miami and canonical_hash(config) != canonical_hash(preparation.records["settings"]):
        raise ValueError("MIAMI_PREPARATION_SETTINGS_CHANGED")
    if (
        authorization.mode != ExecutionMode.LOCAL_PAPER
        or authorization.max_new_positions != 1
        or authorization.max_contracts_per_position != 1
        or not aware(authorization.created_at) <= aware(now) < aware(authorization.expires_at)
    ):
        raise ValueError("CURRENT_ONE_CONTRACT_AUTHORIZATION_REQUIRED")
    engines = (
        preparation.engine_outputs
        if isinstance(preparation, MiamiPreparationResult)
        else preparation
    )
    if engines is None:
        raise ValueError("ACTUAL_PREPARATION_ENGINE_OUTPUTS_REQUIRED")
    paper, sizing, risk = engines.decision, engines.phase3m, engines.phase3n
    output, request = engines.forecast_output, engines.risk_request
    if paper is None or sizing is None or risk is None or output is None or request is None:
        raise ValueError("ACTUAL_PREPARATION_ENGINE_OUTPUTS_REQUIRED")
    records = preparation.records
    at = aware(records["decision_at"])
    if not 0 <= (aware(now) - at).total_seconds() <= 60:
        raise ValueError("PREPARATION_REQUALIFICATION_REQUIRED")
    if (
        paper.ticker != preparation.ticker
        or paper.forecast_id != records["forecast_id"]
        or paper.probability != output.yes_probability
        or paper.model_name != output.model_name
        or records["sizing"] != sizing.as_dict()
        or records["risk"] != risk.as_dict()
        or sizing.decision_timestamp != at
        or risk.decision_timestamp != at
    ):
        raise ValueError("PREPARATION_OUTPUT_IDENTITY_MISMATCH")
    for key in (
        ("forecast_id", "snapshot_id", "sizing_id", "risk_id")
        if miami
        else ("forecast_id", "snapshot_id", "feature_id", "sizing_id", "risk_id")
    ):
        if type(records[key]) is not int or records[key] < 1:
            raise ValueError("PERSISTED_PREPARATION_ID_REQUIRED:" + key)
    model_row = model.decode()
    if (
        model_row.get("model_kind") != "fixed_heuristic"
        or model_row.get("name") != paper.model_name
    ):
        raise ValueError("EXACT_FIXED_WEATHER_MODEL_REQUIRED")
    _verify_model_dependencies(model_row, model_code, repository, miami=miami)
    originals: dict[str, tuple[EvidenceReference, dict[str, Any]]] = {}
    if isinstance(preparation, MiamiPreparationResult):
        assert preparation.original_context is not None and preparation.orderbook is not None
        ctx = preparation.original_context
        assert preparation.orderbook_receipt is not None
        metadata = zip(
            (ctx.market, ctx.event, ctx.series, preparation.orderbook),
            (*ctx.catalog_receipts, preparation.orderbook_receipt),
            strict=True,
        )
        envelopes = []
        for captured_original, captured_receipt in metadata:
            envelope = _artifact(
                dict(
                    url=captured_original.url,
                    received_at=captured_original.received_at.isoformat(),
                    body=captured_original.artifact.decode(),
                    captured_original_response_sha256=captured_original.artifact.sha256,
                    captured_original_response_payload_hex=captured_original.artifact.payload.hex(),
                    original_receipt_sha256=captured_receipt.sha256,
                    original_receipt_payload_hex=captured_receipt.payload.hex(),
                    envelope_origin="DERIVED_FROM_RETAINED_ORIGINAL_RESPONSE_AND_RECEIPT",
                )
            )
            envelopes.append(
                EvidenceReference(
                    "miami-metadata:" + envelope.sha256, envelope.sha256, envelope.payload
                )
            )
        source_envelopes = tuple(envelopes)
    else:
        source_envelopes = preparation.source_envelopes
    for reference in source_envelopes:
        if not reference.valid():
            raise ValueError("ORIGINAL_PREPARATION_ENVELOPE_HASH_MISMATCH")
        row = json.loads(reference.payload)
        if row["url"] in originals:
            raise ValueError("DUPLICATE_PREPARATION_SOURCE")
        originals[row["url"]] = (reference, row)
    market = originals[f"{PUBLIC_BASE}/markets/{paper.ticker}"][1]["body"]["market"]
    event = originals[f"{PUBLIC_BASE}/events/{market['event_ticker']}"][1]["body"]["event"]
    series = originals[f"{PUBLIC_BASE}/series/{event['series_ticker']}"][1]["body"]["series"]
    identity = dict(ticker=paper.ticker, event_id=event["event_ticker"], series=series["ticker"])
    if _development_rule is not None:
        from .miami_development import DevelopmentRuleBinding

        if not miami or type(_development_rule) is not DevelopmentRuleBinding:
            raise ValueError("MIAMI_DEVELOPMENT_RULE_TYPE_REQUIRED")
        _development_rule.validate(identity, market, rule_documents, at)
        policy: rule_verifier.CertifiedRulePolicy | DevelopmentRuleBinding = _development_rule
    else:
        policies = [
            item for item in rule_verifier.CERTIFIED_RULE_POLICIES if item.ticker == paper.ticker
        ]
        if len(policies) != 1 or not rule_documents:
            raise ValueError("NO_UNAMBIGUOUS_CERTIFIED_RULE")
        policy = policies[0]
    analytical = [url for url in originals if url.endswith("/forecast/hourly")]
    if not miami and len(analytical) != 1:
        raise ValueError("EXACT_ANALYTICAL_FORECAST_SOURCE_REQUIRED")
    hourly_url = BUNDLE_URL if miami else analytical[0]
    sources = []
    for url, (reference, original) in originals.items():
        row = dict(original)
        row.update(
            available_at=original["received_at"],
            original_envelope_sha256=reference.sha256,
            original_envelope_payload_hex=reference.payload.hex(),
        )
        if url == hourly_url:
            properties = original["body"]["properties"]
            row.update(
                clock_basis="provider",
                provider_generated_at=properties["generatedAt"],
                provider_updated_at=properties["updateTime"],
            )
        else:
            row.update(
                clock_basis="public_rest_receipt",
                provider_generated_at=None,
                provider_updated_at=None,
            )
        sources.append(_artifact(row))
    miami_verified = None
    if isinstance(preparation, MiamiPreparationResult):
        assert preparation.source_bundle is not None
        miami_verified = verify_miami_provenance_source(
            preparation.source_bundle, decision_at=at, now=now
        )
        sources.append(_artifact(preparation.source_bundle))
        if model_row.get("version") != "1" or aware(model_row["frozen_at"]) > aware(
            miami_verified["inputs"]["model_input_as_of"]
        ):
            raise ValueError("MIAMI_FIXED_MODEL_FREEZE_BINDING")
    source_hashes = [value.sha256 for value in sources]
    source_by_url = {value.decode()["url"]: value for value in sources}
    analytical_source = source_by_url[hourly_url]
    if isinstance(preparation, MiamiPreparationResult):
        assert preparation.source_bundle is not None
        feature_record = miami_feature_record(preparation.source_bundle, decision_at=at, now=now)
    else:
        feature_record = dict(
            name="nws_hourly_input",
            value=analytical_source.decode()["body"]["properties"]["periods"],
            source_sha256=analytical_source.sha256,
            observed_at=analytical_source.decode()["provider_generated_at"],
            available_at=analytical_source.decode()["available_at"],
        )
    features = _artifact(
        identity
        | dict(
            id=records.get("feature_id"),
            source_forecast_id=records.get("source_forecast_id"),
            weather_link_id=records.get("weather_link_id"),
            source_hashes=source_hashes,
            generated_at=records["forecast_generated_at"]
            if miami
            else records["feature_generated_at"],
            available_at=records["forecast_generated_at"]
            if miami
            else records["feature_available_at"],
            records=[feature_record],
            model_feature_json=output.feature_json,
            computed_feature_json=feature_record["value"] if miami else records["features"],
        )
    )
    book_source = source_by_url[f"{PUBLIC_BASE}/markets/{paper.ticker}/orderbook"]
    book_row = book_source.decode()
    if records["book"] != book_row["body"] or aware(request.market_snapshot.captured_at) != aware(
        book_row["received_at"]
    ):
        raise ValueError("ORIGINAL_PREPARATION_BOOK_MISMATCH")
    book = parse_orderbook(book_row["body"])
    if book.best_yes_bid is None or book.best_yes_ask is None:
        raise ValueError("EXECUTABLE_BASELINE_BOOK_REQUIRED")
    baseline = (book.best_yes_bid + book.best_yes_ask) / 2
    common_model = dict(
        model_name=model_row["name"],
        model_version=model_row["version"],
        model_kind="fixed_heuristic",
        training_cutoff=None,
        model_frozen_at=model_row["frozen_at"],
        model_code_sha256=model_row["code_sha256"],
        model_parameters_sha256=model_row["parameters_sha256"],
    )
    forecast = _artifact(
        identity
        | common_model
        | dict(
            id=records["forecast_id"],
            probability=str(output.yes_probability),
            generated_at=records["forecast_generated_at"],
            available_at=records["forecast_available_at"],
            source_hashes=source_hashes,
            model_artifact_sha256=model.sha256,
            features_artifact_sha256=features.sha256,
            code_sha=code_sha,
            rule_version=policy.version,
            original_forecast_output=_json_value(asdict(output)),
            **({"miami_input_sha256": miami_verified["input_sha256"]} if miami_verified else {}),
        )
    )
    snapshot = _artifact(
        identity
        | dict(
            id=records["snapshot_id"],
            book=book_row["body"],
            captured_at=book_row["received_at"],
            available_at=book_row["received_at"],
            market_implied_probability=str(baseline),
        )
    )
    artifacts = dict(
        model=model,
        forecast=forecast,
        snapshot=snapshot,
        config=_artifact(config),
        phase3m=_artifact(sizing.as_dict()),
        phase3n=_artifact(risk.as_dict()),
    )
    costs = records["ev"]
    from kalshi_predictor.paper.fees import CONTRACT_KEY, decision_fee_quote

    fee_quote = decision_fee_quote(
        paper.raw_decision_json,
        ticker=paper.ticker,
        side=paper.side,
        quantity=paper.quantity,
        price=paper.limit_price,
        simulator_floor=settings.paper_default_fee_per_contract,
        now=now,
        required=True,
    )
    assert fee_quote is not None
    if (
        fee_quote.decode()["event_id"] != identity["event_id"]
        or fee_quote.decode()["series"] != identity["series"]
        or records.get("fee_contract") != fee_quote.decode()
        or Decimal(str(costs["estimated_fee"])) != fee_quote.charge
        or request.estimated_round_trip_fees != fee_quote.charge
    ):
        raise ValueError("PREPARATION_FEE_EVIDENCE_OR_RISK_MISMATCH")
    ev = compute_net_ev(
        model_probability=paper.probability if paper.side == "BUY_YES" else 1 - paper.probability,
        executable_price=paper.limit_price,
        estimated_fee=Decimal(str(costs["estimated_fee"])),
        slippage_allowance=Decimal(str(costs["slippage_allowance"])),
        uncertainty_buffer=Decimal(str(costs["uncertainty_buffer"])),
    )
    if _json_value(asdict(ev)) != _json_value(costs) or paper.quantity != 1:
        raise ValueError("PREPARATION_EXECUTABLE_EV_MISMATCH")
    rule_payload = identity | dict(
        rule_version=policy.version,
        policy=_json_value(asdict(policy)),
        documents=[
            dict(url=doc.url, sha256=doc.sha256, payload_hex=doc.payload.hex())
            for doc in rule_documents
        ],
    )
    rule_artifact = _artifact(rule_payload)
    observation_at = aware(policy.observation_time)
    expected = deadline = None
    if isinstance(policy, rule_verifier.CertifiedRulePolicy):
        expected = observation_at + timedelta(seconds=policy.expected_settlement_seconds)
        if policy.review_extension_seconds is None:
            raise ValueError("RULE_SETTLEMENT_FINALITY_UNBOUNDED")
        deadline = observation_at + timedelta(
            seconds=policy.final_settlement_seconds + policy.review_extension_seconds
        )
    inputs = (
        identity
        | common_model
        | dict(
            category=series["category"],
            model_evaluation_head_sha256=model_evaluation_head_sha256,
            station=None if miami else "KNYC",
            forecast_id=paper.forecast_id,
            snapshot_id=records["snapshot_id"],
            feature_id=records.get("feature_id"),
            sizing_id=records["sizing_id"],
            risk_id=records["risk_id"],
            position_sizing_decision_id=records["sizing_id"],
            advanced_risk_decision_id=records["risk_id"],
            decision_at=at.isoformat(),
            close_time=market["close_time"],
            side=paper.side,
            forecast_probability=str(paper.probability),
            executable_price=str(paper.limit_price),
            estimated_fee=str(ev.estimated_fee),
            guarded_fee_contract=fee_quote.decode(),
            slippage=str(ev.slippage_allowance),
            uncertainty=str(ev.uncertainty_buffer),
            source_hashes=source_hashes,
            settings=config,
            config_hash=canonical_hash(config),
            code_sha=code_sha,
            rule_version=policy.version,
            settlement_rule=_json_value(asdict(policy)),
            observation_time=policy.observation_time,
            market_open_time=market["open_time"],
            market_close_time=market["close_time"],
            expected_expiration_time=market.get("expected_expiration_time"),
            latest_expiration_time=market.get("latest_expiration_time"),
            final_settlement_time=None,
            expected_settlement_time=None if expected is None else expected.isoformat(),
            settlement_deadline=None if deadline is None else deadline.isoformat(),
            latest_settlement_at=None if deadline is None else deadline.isoformat(),
            rule_artifact_sha256=rule_artifact.sha256,
            features_artifact_sha256=features.sha256,
            snapshot_book_hash=canonical_hash(book_row["body"]),
            phase3m_hash=canonical_hash(sizing.as_dict()),
            phase3n_hash=canonical_hash(risk.as_dict()),
            authorization_sha256=authorization_fingerprint(authorization),
            market_rules_hash=canonical_hash(
                dict(
                    primary=market.get("rules_primary"),
                    secondary=market.get("rules_secondary"),
                    contract_terms_url=series.get("contract_terms_url"),
                )
            ),
        )
    )
    if miami_verified is not None:
        for key in (
            "source_kind",
            "historical_public_availability",
            "miami_context_sha256",
            "model_input_as_of",
            "origin_at",
            "frozen_prediction_sha256",
        ):
            inputs[key] = miami_verified["inputs"][key]
        inputs["miami_input_sha256"] = miami_verified["input_sha256"]
        if aware(inputs["observation_time"]) != aware(miami_verified["inputs"]["observation_time"]):
            raise ValueError("MIAMI_RULE_TARGET_MISMATCH")
    inputs["feature_timestamps"] = [
        {key: value for key, value in feature_record.items() if key != "value"}
    ]
    inputs["source_timestamps"] = [
        dict(
            sha256=value.sha256,
            **{
                key: value.decode()[key]
                for key in (
                    "provider_updated_at",
                    "provider_generated_at",
                    "available_at",
                    "received_at",
                    "clock_basis",
                )
            },
        )
        for value in sources
    ]
    inputs["original_engine_inputs"] = _json_value(
        dict(
            sizing_evidence=records["sizing_evidence"],
            risk_request=asdict(request),
        )
    )
    inputs.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    decision_id = canonical_hash(inputs)
    provenance = ProvenanceContext(
        artifacts, tuple(sources), (), model_code, features, code_sha, policy.version
    )
    if _development_rule is not None:
        return build_observation(
            provenance_args=dict(
                decision=inputs, decision_id=decision_id, context=provenance,
                now=now, phase3m=sizing, phase3n=risk,
            ),
            independent_event_id=identity["event_id"],
            event_window_start=aware(market["open_time"]),
            event_window_end=observation_at,
            rule_artifact=rule_artifact,
            market_probability=float(baseline),
            executable_price=float(ev.executable_price),
            estimated_fee=float(ev.estimated_fee),
            slippage=float(ev.slippage_allowance),
            uncertainty=float(ev.uncertainty_buffer),
        )
    verified_rule = rule_verifier.verify_settlement_rule(
        decision=inputs, documents=rule_documents, registry=rule_verifier.CERTIFIED_RULE_POLICIES
    )
    timing = verify_settlement_horizon(decision=inputs, rule=verified_rule, now=now)
    if not verified_rule.passed or not timing.passed:
        raise ValueError(
            "ASSEMBLY_RULE_OR_TIMING_INVALID:" + ",".join(verified_rule.blockers + timing.blockers)
        )
    checked = verify_complete_provenance(
        decision=inputs,
        decision_id=decision_id,
        context=provenance,
        now=now,
        phase3m=sizing,
        phase3n=risk,
    )
    if not checked.passed:
        raise ValueError("ASSEMBLY_PROVENANCE_INVALID:" + ",".join(checked.blockers))
    context = QualificationContext(repository, rule_documents, provenance, sizing, risk)
    references = tuple(
        EvidenceReference("assembled-original:" + value.sha256, value.sha256, value.payload)
        for value in sources
    )
    evidence = []
    for gate in sorted(COLLECTOR_GATES):
        verifier = MIAMI_VERIFIER if miami and gate == 4 else SEMANTIC_VERIFIERS[gate]
        gate_references = references
        gate_context: object = context
        if miami and gate == 4:
            gate_references = (
                EvidenceReference(
                    "miami-original-bundle", analytical_source.sha256, analytical_source.payload
                ),
            )
            gate_context = MiamiBundleGateContext(analytical_source)
        report = _artifact(
            dict(
                schema="overnight-paper-gate-v1",
                gate=gate,
                decision_id=decision_id,
                ticker=paper.ticker,
                category=series["category"],
                verifier=verifier,
                verdict="PASS",
                sources=[r.sha256 for r in gate_references],
                validated_at=at.isoformat(),
                valid_until=min(
                    at + timedelta(seconds=60), aware(market["close_time"])
                ).isoformat(),
            )
        )
        evidence.append(
            GateEvidence(
                gate,
                decision_id,
                series["category"],
                paper.ticker,
                verifier,
                EvidenceReference("semantic-report:" + str(gate), report.sha256, report.payload),
                sources=gate_references,
                context=gate_context,
            )
        )
    args = dict(
        ticker=paper.ticker,
        category=series["category"],
        decision_inputs=inputs,
        decision_id=decision_id,
        evidence=tuple(evidence),
        ev=ev,
        minimum_net_ev=settings.paper_min_edge,
        phase3m=sizing,
        phase3n=risk,
        mode=ExecutionMode.LOCAL_PAPER,
    )
    qualification = qualify_candidate(**args)
    assert expected is not None and deadline is not None
    shadow = dict(
        ticker=paper.ticker,
        event_ticker=identity["event_id"],
        series_ticker=identity["series"],
        model=paper.model_name,
        model_version=model_row["version"],
        forecast=str(paper.probability),
        snapshot=book_row["body"],
        price=str(paper.limit_price),
        net_ev=str(ev.net_ev),
        sizing=sizing.as_dict(),
        risk=risk.as_dict(),
        source_provenance=[value.decode() for value in sources],
        settlement_rule_version=policy.version,
        decision_at=at.isoformat(),
        forecast_at=records["forecast_generated_at"],
        # Legacy shadow timestamp is interpreted by its explicit clock basis.
        source_updated_at=analytical_source.decode()[
            "available_at" if miami else "provider_updated_at"
        ],
        snapshot_at=book_row["received_at"],
        close_time=market["close_time"],
        side=paper.side,
        expected_settlement_at=expected.isoformat(),
        latest_settlement_at=deadline.isoformat(),
        qualification_inputs=inputs,
        qualification_status=qualification.status.value,
        qualification_blockers=list(qualification.blockers),
        model_evaluation_required=True,
    )
    if miami:
        source_clock = analytical_source.decode()
        shadow.update(
            source_clock_basis=source_clock["clock_basis"],
            source_available_at=source_clock["available_at"],
            source_provider_updated_at=source_clock["provider_updated_at"],
        )
    dataset = (
        build_observation(
            provenance_args=dict(
                decision=inputs,
                decision_id=decision_id,
                context=provenance,
                now=now,
                phase3m=sizing,
                phase3n=risk,
            ),
            independent_event_id=identity["event_id"],
            event_window_start=aware(market["open_time"]),
            event_window_end=observation_at,
            rule_artifact=rule_artifact,
            market_probability=float(baseline),
            executable_price=float(ev.executable_price),
            estimated_fee=float(ev.estimated_fee),
            slippage=float(ev.slippage_allowance),
            uncertainty=float(ev.uncertainty_buffer),
        )
        if include_evaluation_observation
        else None
    )
    original_decision = dict(paper.raw_decision_json)
    original_decision[CONTRACT_KEY] = fee_quote.decode()
    for key, value in (
        ("position_sizing_decision_id", records["sizing_id"]),
        ("advanced_risk_decision_id", records["risk_id"]),
    ):
        if key in original_decision and original_decision[key] != value:
            raise ValueError("PREPARATION_ENGINE_RECORD_LINK_MISMATCH")
        original_decision[key] = value
    linked_paper = replace(paper, raw_decision_json=original_decision)
    return PreparedCandidate(linked_paper, args, shadow, dataset)


def assemble_weather_candidate(
    *,
    preparation: WeatherPreparationResult,
    model: Artifact | None,
    model_code: bytes,
    settings: Settings,
    repository: Path,
    code_sha: str,
    authorization: LocalPaperAuthorization,
    rule_documents: tuple[RuleDocument, ...],
    now: datetime,
    include_evaluation_observation: bool = True,
    model_evaluation_head_sha256: str | None = None,
) -> PreparedCandidate:
    if type(preparation) is not WeatherPreparationResult:
        raise ValueError("COMPLETED_WEATHER_PREPARATION_REQUIRED")
    candidate = _assemble_candidate(
        preparation=preparation,
        model=model,
        model_code=model_code,
        settings=settings,
        repository=repository,
        code_sha=code_sha,
        authorization=authorization,
        rule_documents=rule_documents,
        now=now,
        include_evaluation_observation=include_evaluation_observation,
        model_evaluation_head_sha256=model_evaluation_head_sha256,
    )
    if type(candidate) is not PreparedCandidate:
        raise ValueError("ADMISSION_CANDIDATE_REQUIRED")
    return candidate


def assemble_miami_candidate(
    *,
    session: Session,
    preparation: MiamiPreparationResult,
    model: Artifact | None,
    model_code: bytes,
    settings: Settings,
    repository: Path,
    code_sha: str,
    authorization: LocalPaperAuthorization,
    rule_documents: tuple[RuleDocument, ...],
    now: datetime,
    include_evaluation_observation: bool = True,
    model_evaluation_head_sha256: str | None = None,
) -> PreparedCandidate:
    if type(preparation) is not MiamiPreparationResult:
        raise ValueError("COMPLETED_MIAMI_PREPARATION_REQUIRED")
    candidate = _assemble_candidate(
        preparation=preparation,
        model=model,
        model_code=model_code,
        settings=settings,
        repository=repository,
        code_sha=code_sha,
        authorization=authorization,
        rule_documents=rule_documents,
        now=now,
        include_evaluation_observation=include_evaluation_observation,
        model_evaluation_head_sha256=model_evaluation_head_sha256,
        miami_session=session,
    )
    if type(candidate) is not PreparedCandidate:
        raise ValueError("ADMISSION_CANDIDATE_REQUIRED")
    return candidate
