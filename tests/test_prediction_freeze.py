import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from kalshi_predictor.crypto import research_provenance
from kalshi_predictor.crypto.independent_evaluation import (
    PredictionRecord,
    evaluate_independent_models,
)
from kalshi_predictor.crypto.research_provenance import freeze_prediction, verify_unchanged

NOW = datetime(2026, 9, 10, 20, tzinfo=UTC)


def freeze(path, *, clock, **overrides):
    kwargs = dict(
        model_input_as_of=NOW,
        input_received_at=NOW - timedelta(seconds=1),
        model_committed_at=NOW - timedelta(minutes=1),
        target_at=NOW + timedelta(hours=1),
    )
    kwargs.update(overrides)
    return freeze_prediction(
        path, {"probabilities": {"normal": 0.6, "student": 0.55}}, clock=clock, **kwargs
    )


def test_disk_freeze_precedes_receipt_and_decision_and_supports_paired_evaluation(
    tmp_path, monkeypatch
):
    events = []
    original_fsync = research_provenance.os.fsync

    def fsync(fd):
        original_fsync(fd)
        events.append("fsync")

    times = iter((NOW + timedelta(seconds=1), NOW + timedelta(seconds=2)))

    def clock():
        events.append("clock")
        return next(times)

    monkeypatch.setattr(research_provenance.os, "fsync", fsync)
    proof = freeze(tmp_path / "prediction", clock=clock)
    assert events == ["fsync", "clock", "fsync", "clock", "fsync"]
    raw = (tmp_path / "prediction" / "prediction.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == proof["prediction_sha256"]
    assert proof["model_input_as_of"] == NOW.isoformat()
    assert proof["prediction_recorded_at"] < proof["decision_at"]
    assert json.loads(raw)["prediction"]["probabilities"]["normal"] == 0.6
    records = [
        PredictionRecord(
            event_id="event",
            target_id="target",
            model=model,
            symbol="SOL",
            horizon="hourly",
            decision_at=datetime.fromisoformat(proof["decision_at"]),
            model_committed_at=datetime.fromisoformat(proof["model_committed_at"]),
            input_received_at=datetime.fromisoformat(proof["input_received_at"]),
            prediction_recorded_at=datetime.fromisoformat(proof["prediction_recorded_at"]),
            settlement_known_at=NOW + timedelta(hours=1),
            prediction_sha256=proof["prediction_sha256"],
            settlement_sha256="b" * 64,
            probability=p,
            outcome=1,
        )
        for model, p in (("normal", 0.6), ("student", 0.55))
    ]
    result = evaluate_independent_models(
        records, models=("normal", "student"), as_of=NOW + timedelta(hours=2)
    )
    assert result["independent_event_n"] == 1


@pytest.mark.parametrize("which", ["prediction", "receipt", "decision"])
def test_io_failure_never_returns_valid_decision(tmp_path, monkeypatch, which):
    count = 0
    fail_at = {"prediction": 1, "receipt": 2, "decision": 3}[which]

    def fsync(_fd):
        nonlocal count
        count += 1
        if count == fail_at:
            raise OSError("disk failure")

    monkeypatch.setattr(research_provenance.os, "fsync", fsync)
    with pytest.raises(OSError, match="disk failure"):
        freeze(tmp_path / "prediction", clock=lambda: NOW + timedelta(seconds=1))
    assert not (tmp_path / "prediction" / "decision.json").exists()


@pytest.mark.parametrize("seconds,error", [(-1, "RECORDING_CLOCK"), (3600, "TARGET_EXPIRED")])
def test_clock_regression_or_expiry_cannot_record_decision(tmp_path, seconds, error):
    with pytest.raises(ValueError, match=error):
        freeze(tmp_path / "prediction", clock=lambda: NOW + timedelta(seconds=seconds))
    assert not (tmp_path / "prediction" / "decision.json").exists()


def test_receipt_future_to_model_cutoff_rejected_before_creating_artifacts(tmp_path):
    path = tmp_path / "prediction"
    with pytest.raises(ValueError, match="INPUT_CHRONOLOGY"):
        freeze(path, clock=lambda: NOW, input_received_at=NOW + timedelta(seconds=1))
    assert not path.exists()


def test_existing_prediction_cannot_be_overwritten(tmp_path):
    path = tmp_path / "prediction"
    freeze(path, clock=lambda: NOW + timedelta(seconds=1))
    raw = (path / "prediction.json").read_bytes()
    with pytest.raises(FileExistsError):
        freeze(path, clock=lambda: NOW + timedelta(seconds=2))
    assert (path / "prediction.json").read_bytes() == raw


def test_model_source_change_is_rejected_before_freezing_next_prediction(tmp_path):
    model = tmp_path / "model.py"
    model.write_bytes(b"original")
    proof = {"files": [{"path": "model.py", "sha256": hashlib.sha256(b"original").hexdigest()}]}
    model.write_bytes(b"changed")
    with pytest.raises(ValueError, match="MODEL_CHANGED_DURING_CAPTURE"):
        verify_unchanged(tmp_path, proof)
