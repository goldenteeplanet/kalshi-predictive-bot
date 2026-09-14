import hashlib
import json
import sys
from datetime import UTC, datetime
from types import ModuleType

import pytest

from kalshi_predictor.overnight_paper import provenance as p


def artifact(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return p.Artifact(hashlib.sha256(raw).hexdigest(), raw)


def provenance_inputs(*, change=None, source_available_at=None, training_label_at=None):
    from test_phase_3n_advanced_risk import _config, _request

    from kalshi_predictor.advanced_risk.engine import AdvancedRiskEngine
    from kalshi_predictor.position_sizing.sizer import (
        DynamicPositionSizer,
        PositionSizingConfig,
        PositionSizingInput,
    )

    stamp = "2026-09-08T01:00:00Z"
    training_stamp = "2026-09-07T00:00:00Z"
    model_code = b"# Synthetic model code fixture, never a real model readiness certificate\n"
    training = artifact(
        {
            "records": [
                {
                    "available_at": training_stamp,
                    "label_available_at": training_label_at or training_stamp,
                    "source_payload": {"feature": 1},
                    "label_payload": {"label": "yes"},
                    "source_sha256": p.canonical_hash({"feature": 1}),
                    "label_source_sha256": p.canonical_hash({"label": "yes"}),
                }
            ]
        }
    )
    source = artifact(
        {
            "body": {"feature": 2},
            "url": "https://example.org/fixture",
            "provider_generated_at": stamp,
            "provider_updated_at": stamp,
            "available_at": source_available_at or stamp,
            "received_at": stamp,
        }
    )
    source_hashes = [source.sha256]
    model = {
        "version": "fixture-v1",
        "code_sha256": hashlib.sha256(model_code).hexdigest(),
        "training_dataset_hashes": [training.sha256],
        "training_cutoff": training_stamp,
        "created_at": training_stamp,
        "available_at": training_stamp,
    }
    model_artifact = artifact(model)
    common = {"ticker": "T", "event_id": "E", "series": "S"}
    clock = datetime(2026, 9, 8, 1, tzinfo=UTC)
    phase3m = DynamicPositionSizer(PositionSizingConfig()).decide(
        PositionSizingInput(
            confidence_score=0.6,
            opportunity_score=0.6,
            liquidity_score=0.8,
            current_drawdown_fraction=0,
            max_drawdown_fraction=0.2,
            historical_accuracy=0.6,
            historical_sample_size=30,
            decision_timestamp=clock,
        )
    )
    phase3n = AdvancedRiskEngine(_config()).decide(_request(decision_timestamp=clock))
    rows = {
        "model": model,
        "forecast": common
        | {
            "id": 1,
            "probability": "0.6",
            "generated_at": stamp,
            "available_at": stamp,
            "source_hashes": source_hashes,
            "model_version": "fixture-v1",
            "model_artifact_sha256": model_artifact.sha256,
        },
        "snapshot": common
        | {"id": 2, "book": {"fixture": 1}, "captured_at": stamp, "available_at": stamp},
        "config": {"minimum_edge": "0.05"},
        "phase3m": phase3m.as_dict(),
        "phase3n": phase3n.as_dict(),
    }
    if change:
        change(rows)
    artifacts = {key: artifact(value) for key, value in rows.items()}
    decision = common | {
        "forecast_id": 1,
        "snapshot_id": 2,
        "model_version": "fixture-v1",
        "model_code_sha256": model["code_sha256"],
        "forecast_probability": "0.6",
        "source_hashes": source_hashes,
        "decision_at": stamp,
        "close_time": "2026-09-08T02:00:00Z",
        "config_hash": p.canonical_hash(rows["config"]),
        "snapshot_book_hash": p.canonical_hash({"fixture": 1}),
        "phase3m_hash": p.canonical_hash(rows["phase3m"]),
        "phase3n_hash": p.canonical_hash(rows["phase3n"]),
    }
    decision.update({key + "_artifact_sha256": value.sha256 for key, value in artifacts.items()})
    return dict(
        decision=decision,
        decision_id=p.canonical_hash(decision),
        artifacts=artifacts,
        source_artifacts=(source,),
        training_artifacts=(training,),
        model_code=model_code,
        now=datetime(2026, 9, 8, 1, tzinfo=UTC),
        phase3m=phase3m,
        phase3n=phase3n,
    )


def test_complete_provenance_does_not_certify_model_skill_or_rules():
    result = p.verify_full_provenance(**provenance_inputs())
    assert result.passed
    assert not result.model_calibration_verified and not result.settlement_rules_verified
    assert result.scope == "PROVENANCE_COMPLETENESS_ONLY"


@pytest.mark.parametrize(
    "role,field,value",
    [
        ("forecast", "probability", "0.9"),
        ("snapshot", "id", 999),
        ("model", "version", "other"),
        ("forecast", "source_hashes", []),
        ("snapshot", "available_at", "2026-09-08T01:00:01Z"),
        ("forecast", "available_at", "2026-09-08T01:00:01Z"),
        ("model", "training_cutoff", "2026-09-08T01:00:01Z"),
        ("phase3n", "decision_timestamp", "2026-09-08T01:00:01Z"),
    ],
)
def test_rehashed_but_inconsistent_or_future_original_data_is_rejected(role, field, value):
    args = provenance_inputs(change=lambda rows: rows[role].update({field: value}))
    assert not p.verify_full_provenance(**args).passed


def test_unavailable_training_records_and_original_code_cannot_be_attested():
    args = provenance_inputs()
    assert not p.verify_full_provenance(**(args | {"training_artifacts": ()})).passed
    assert not p.verify_full_provenance(**(args | {"model_code": b"different"})).passed


def test_original_source_tampering_fails_even_when_decision_unchanged():
    args = provenance_inputs()
    forged = p.Artifact(args["source_artifacts"][0].sha256, b'{"PASS":true}')
    assert not p.verify_full_provenance(**(args | {"source_artifacts": (forged,)})).passed


def test_provider_event_time_does_not_hide_later_source_visibility():
    result = p.verify_full_provenance(
        **provenance_inputs(
            source_available_at="2026-09-08T01:00:01Z",
        )
    )
    assert not result.passed
    assert "FUTURE_OR_INCONSISTENT_SOURCE_VISIBILITY" in result.blockers


def test_training_labels_must_already_exist_at_frozen_training_cutoff():
    result = p.verify_full_provenance(
        **provenance_inputs(
            training_label_at="2026-09-07T00:00:01Z",
        )
    )
    assert not result.passed
    assert "TRAINING_DATA_NOT_VISIBLE_AT_CUTOFF" in result.blockers


def boundary_fixture(tmp_path, monkeypatch):
    """Synthetic reviewed source tree; tests do not certify the running bot release."""
    sha = "a" * 40
    names = {
        "kalshi_predictor.overnight_paper.activation": "activate_local_paper",
        "kalshi_predictor.paper.ledger": "create_paper_order",
        "kalshi_predictor.paper.simulator": "simulate_immediate_fill",
        "kalshi_predictor.position_sizing.service": "size_paper_decision",
    }
    pinned, modules = {}, {}
    for name, function in names.items():
        path = tmp_path / ("src/" + name.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = f"def {function}():\n    return None\n".encode()
        path.write_bytes(raw)
        module = ModuleType(name)
        module.__file__ = str(path)
        exec(compile(raw, str(path), "exec"), module.__dict__)
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
        pinned[name] = hashlib.sha256(raw).hexdigest()
    activation = modules["kalshi_predictor.overnight_paper.activation"]
    for name, function in names.items():
        setattr(activation, function, getattr(modules[name], function))
    monkeypatch.setattr(p, "AUDITED_BOUNDARY_SHA256", pinned)
    monkeypatch.setattr(p, "_repository", lambda: tmp_path)

    def git(command, **kwargs):
        if command[3:] == ["rev-parse", "HEAD"]:
            return sha.encode()
        if command[3:] == ["status", "--porcelain"]:
            return b""
        return (tmp_path / command[-1].split(":", 1)[1]).read_bytes()

    monkeypatch.setattr(p.subprocess, "check_output", git)
    settings = dict(
        execution_enabled=False,
        execution_dry_run=True,
        execution_kill_switch=True,
        execution_gateway_mode="disabled",
        autopilot_enabled=False,
        autopilot_dry_run=True,
        learning_mode=False,
    )
    return dict(repository=tmp_path, code_sha=sha, settings=settings), modules


def test_reviewed_runtime_origins_flags_and_commit_bytes_are_checked(tmp_path, monkeypatch):
    args, modules = boundary_fixture(tmp_path, monkeypatch)
    assert p.verify_local_boundary(**args).passed
    modules["kalshi_predictor.paper.ledger"].__file__ = str(tmp_path / "other.py")
    assert not p.verify_local_boundary(**args).passed


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_enabled", True),
        ("execution_dry_run", False),
        ("execution_kill_switch", False),
        ("execution_gateway_mode", "live"),
        ("autopilot_enabled", True),
        ("learning_mode", True),
    ],
)
def test_actual_unsafe_settings_cannot_be_overridden_by_manifest(
    tmp_path, monkeypatch, field, value
):
    args, _ = boundary_fixture(tmp_path, monkeypatch)
    args["settings"][field] = value
    assert not p.verify_local_boundary(**args).passed


def test_reviewed_source_change_and_runtime_alias_replacement_fail(tmp_path, monkeypatch):
    args, modules = boundary_fixture(tmp_path, monkeypatch)
    activation = modules["kalshi_predictor.overnight_paper.activation"]
    activation.create_paper_order = lambda: None
    assert not p.verify_local_boundary(**args).passed
    activation.create_paper_order = modules["kalshi_predictor.paper.ledger"].create_paper_order
    path = tmp_path / "src/kalshi_predictor/paper/ledger.py"
    path.write_text("# changed\n")
    assert not p.verify_local_boundary(**args).passed
