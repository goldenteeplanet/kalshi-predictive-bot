# ruff: noqa: F811
"""Real owned SQLite preparation and prospective dataset; no admission or network."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from test_miami_owned_preparation import owned_inputs  # noqa: F401
from test_miami_preparation import prepared_inputs  # noqa: F401
from test_miami_source_gate import artifact, context, fixtures, grid_context  # noqa: F401
from test_overnight_activation import baseline_template  # noqa: F401

from kalshi_predictor.overnight_paper import miami_development as module
from kalshi_predictor.overnight_paper import miami_preparation as prep
from kalshi_predictor.overnight_paper import miami_preparation_runner as runner
from kalshi_predictor.overnight_paper import rule_verifier
from kalshi_predictor.overnight_paper.candidate_assembly import (
    MIAMI_MODEL_ENTRYPOINT,
    assemble_miami_candidate,
    miami_model_code_bundle,
)
from kalshi_predictor.overnight_paper.dataset_store import load_dataset
from kalshi_predictor.overnight_paper.miami_storage import owned_miami_factory
from kalshi_predictor.overnight_paper.provenance import canonical_hash
from kalshi_predictor.overnight_paper.rule_verifier import RuleDocument
from kalshi_predictor.overnight_paper.runtime_owner import acquire_runtime_owner
from kalshi_predictor.overnight_paper.watcher import (
    PublicMarketObservation,
    pending_dataset_tickers,
    reconcile_public_settlements,
)


@pytest.fixture
def development(owned_inputs, monkeypatch):
    args, _ = owned_inputs
    args = dict(args, slippage_allowance=Decimal(1), uncertainty_buffer=Decimal(1))
    args["settings"] = args["settings"].model_copy(
        update={"dynamic_position_sizing_external_risk_cap": 0}
    )
    monkeypatch.setattr(module, "utc_now", prep.utc_now)
    monkeypatch.setattr(rule_verifier, "CERTIFIED_RULE_POLICIES", ())
    with acquire_runtime_owner(args["database_path"]) as owner:
        _, storage = owned_miami_factory(
            args["session_factory"],
            database_path=args["database_path"],
            owner=owner,
            authorization=args["authorization"],
        )
        monkeypatch.setattr(module, "utc_now", lambda: fixtures.ORIGIN)
        protocol = module.freeze_miami_development_protocol(
            storage=storage, scenario_total=Decimal("1.01")
        )
        monkeypatch.setattr(module, "utc_now", prep.utc_now)
        result = runner.run_miami_preparation_live_cycle(**args, runtime_owner=owner).live_result
        assert result is not None and result.state == "COMPUTED_UNQUALIFIED"
        assert Decimal(str(result.records["ev"]["net_ev"])) < 0
        repository = Path(__file__).resolve().parents[1]
        dependencies, code = miami_model_code_bundle(repository)
        frozen = fixtures.ORIGIN.isoformat()
        config = args["settings"].model_dump(mode="json")
        model_row = dict(
            name=result.engine_outputs.forecast_output.model_name,
            version="1",
            model_kind="fixed_heuristic",
            training_cutoff=None,
            training_dataset_hashes=[],
            created_at=frozen,
            frozen_at=frozen,
            available_at=frozen,
            parameters=config,
            parameters_sha256=canonical_hash(config),
            code_sha256=hashlib.sha256(code).hexdigest(),
            code_dependencies=dependencies,
            model_entrypoint=MIAMI_MODEL_ENTRYPOINT,
        )
        from kalshi_predictor.overnight_paper.provenance import Artifact

        raw_model = json.dumps(model_row, sort_keys=True, separators=(",", ":")).encode()
        model = Artifact(hashlib.sha256(raw_model).hexdigest(), raw_model)
        document = RuleDocument(module.TERMS_URL, b"synthetic test-only original terms")
        monkeypatch.setattr(module, "TERMS_SHA256", document.sha256)
        receipt = artifact(
            dict(
                url=document.url,
                method="GET",
                requested_at=frozen,
                received_at=frozen,
                status=200,
                bytes=len(document.payload),
                sha256=document.sha256,
            )
        )
        monkeypatch.setattr(module, "TERMS_RECEIPT_SHA256", receipt.sha256)
        rule = module.DevelopmentRuleBinding(
            result.ticker,
            result.ticker.rsplit("-", 1)[0],
            "KXTEMPMIAH",
            result.records["observation_time"],
            receipt.sha256,
            receipt.payload.hex(),
        )
        yield dict(
            preparation=result,
            model=model,
            model_code=code,
            settings=args["settings"],
            repository=repository,
            code_sha="a" * 40,
            rule=rule,
            rule_documents=(document,),
            cost_protocol=protocol,
        )


def chain(args):
    with Session(args["preparation"].owned_storage.engine) as session:
        return load_dataset(session, dataset="paper-release")


def test_owned_development_appends_without_certified_rule_or_paper(development):
    result = module.append_miami_development(**development)
    assert result.admission_authority is False and result.orders_created == 0
    records = chain(development)
    row = records[0].decode()["record"]
    assert row["kind"] == "observation-v1"
    assert row["decision"]["forecast_id"] == result.forecast_id
    assert row["decision"]["snapshot_id"] == result.snapshot_id
    assert row["decision"]["settlement_deadline"] is None
    assert row["development"]["calibrated"] is False
    assert row["development"]["rule_certified"] is False
    assert development["preparation"].engine_outputs.phase3m.live_candidate_contracts == 0
    assert row["decision"]["forecast_probability"] == str(
        development["preparation"].engine_outputs.forecast_output.yes_probability
    )
    with Session(development["preparation"].owned_storage.engine) as session:
        for table in ("paper_orders", "paper_fills", "overnight_shadow"):
            assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
        with pytest.raises(ValueError, match="NO_UNAMBIGUOUS_CERTIFIED_RULE"):
            assemble_miami_candidate(
                session=session,
                preparation=development["preparation"],
                model=development["model"],
                model_code=development["model_code"],
                settings=development["settings"],
                repository=development["repository"],
                code_sha=development["code_sha"],
                authorization=development["preparation"].owned_storage.authorization,
                rule_documents=development["rule_documents"],
                now=prep.utc_now(),
            )


def test_actual_watcher_joins_dataset_only_final_once(development):
    module.append_miami_development(**development)
    prep_result = development["preparation"]
    storage = prep_result.owned_storage
    with Session(storage.engine) as session:
        assert pending_dataset_tickers(session) == frozenset({prep_result.ticker})
    target = fixtures.datetime.fromisoformat(prep_result.records["observation_time"])
    market = prep_result.original_context.market.artifact.decode()["market"]
    market = dict(
        market,
        status="finalized",
        result="no",
        is_provisional=False,
        settlement_ts=(target + timedelta(minutes=2)).isoformat(),
    )
    raw = json.dumps(dict(market=market)).encode()
    source = PublicMarketObservation(
        prep_result.ticker,
        prep_result.original_context.market.url,
        target + timedelta(minutes=3),
        hashlib.sha256(raw).hexdigest(),
        raw,
    )
    from sqlalchemy.orm import sessionmaker

    for _ in range(2):
        report = reconcile_public_settlements(
            session_factory=sessionmaker(storage.engine),
            database_path=storage.database_path,
            observations=(source,),
            now=source.captured_at,
        )
        assert report.shadow_evaluations_created == report.paper_evaluations_created == 0
    rows = chain(development)
    assert len(rows) == 2
    assert rows[1].decode()["record"]["kind"] == "outcome-v1"
    with Session(storage.engine) as session:
        assert pending_dataset_tickers(session) == frozenset()
        assert session.execute(text("SELECT count(*) FROM paper_orders")).scalar_one() == 0


@pytest.mark.parametrize(
    "change",
    [
        "terms",
        "receipt",
        "target",
        "future_target",
        "model",
        "forecast",
        "cost",
        "late_protocol",
        "bool_protocol",
        "expired",
    ],
)
def test_bad_original_or_clock_never_appends(development, monkeypatch, change):
    args = dict(development)
    if change == "terms":
        args["rule_documents"] = (replace(args["rule_documents"][0], payload=b"changed"),)
    elif change == "receipt":
        args["rule"] = replace(args["rule"], receipt_sha256="0" * 64)
    elif change == "target":
        args["rule"] = replace(args["rule"], observation_time=fixtures.ORIGIN.isoformat())
    elif change == "future_target":
        target = fixtures.datetime.fromisoformat(args["rule"].observation_time) + timedelta(hours=1)
        args["rule"] = replace(args["rule"], observation_time=target.isoformat())
    elif change == "model":
        args["model_code"] += b"changed"
    elif change == "forecast":
        args["preparation"].records["forecast"]["yes_probability"] = ".99"
    elif change in {"cost", "late_protocol", "bool_protocol"}:
        row = args["cost_protocol"].decode()
        if change == "cost":
            row["scenario_total"] = ".42"
        elif change == "bool_protocol":
            row["calibrated"] = 0
        else:
            row["committed_at"] = prep.utc_now().isoformat()
        args["cost_protocol"] = artifact(row)
    else:
        now = prep.utc_now() + timedelta(minutes=2)
        monkeypatch.setattr(module, "utc_now", lambda: now)
    with pytest.raises((ValueError, KeyError)):
        module.append_miami_development(**args)
    assert chain(args) == ()


def test_append_failure_rolls_back_development_only(development, monkeypatch):
    original = module.persist_dataset_record

    def failed(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError("synthetic commit failure")

    monkeypatch.setattr(module, "persist_dataset_record", failed)
    with pytest.raises(ValueError, match="synthetic commit failure"):
        module.append_miami_development(**development)
    assert chain(development) == ()


@pytest.mark.parametrize("change", ["missing", "changed_record", "after_receipt"])
def test_protocol_requires_real_prior_same_ledger_record(development, monkeypatch, change):
    args = dict(development)
    storage = args["preparation"].owned_storage
    key = "development-cost-protocol:" + args["cost_protocol"].sha256
    if change == "after_receipt":
        context = args["preparation"].original_context
        late = context.captures[context.current_capture].index.received_at + timedelta(
            microseconds=1
        )
        monkeypatch.setattr(module, "utc_now", lambda: late)
        args["cost_protocol"] = module.freeze_miami_development_protocol(
            storage=storage, scenario_total=Decimal("1.01")
        )
        monkeypatch.setattr(module, "utc_now", prep.utc_now)
    else:
        with Session(storage.engine) as session:
            if change == "missing":
                session.execute(
                    text("DELETE FROM overnight_sprint_cycles WHERE id=:id"), {"id": key}
                )
            else:
                session.execute(
                    text("UPDATE overnight_sprint_cycles SET payload='{}' WHERE id=:id"),
                    {"id": key},
                )
            session.commit()
    with pytest.raises(ValueError):
        module.append_miami_development(**args)
    assert chain(args) == ()
