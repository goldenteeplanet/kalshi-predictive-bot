import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_predictor.crypto.research_provenance import freeze_prediction
from kalshi_predictor.weather.miami_index import STATIONS

spec = importlib.util.spec_from_file_location(
    "miami_outcomes_cli",
    Path(__file__).resolve().parents[1] / "scripts/positive_ev_miami_outcomes.py",
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
ORIGIN = datetime(2026, 9, 10, 22, tzinfo=UTC)


@pytest.fixture
def frozen(tmp_path):
    models = {
        name: {"mean_f": 85, "samples_f": [85]}
        for name in (
            "persistence",
            "fixed_30min_linear_trend",
            "prior_day_increment_empirical",
        )
    }
    forecasts = [
        {
            "units": "fahrenheit",
            "origin_at": ORIGIN.isoformat(),
            "target_at": (ORIGIN + timedelta(minutes=h)).isoformat(),
            "horizon_minutes": h,
            "model_input_as_of": (ORIGIN + timedelta(minutes=1)).isoformat(),
            "input_received_at": ORIGIN.isoformat(),
            "models": models,
            "source_hashes": ["a" * 64],
            "calibration_hashes": ["b" * 64],
        }
        for h in (30, 60)
    ]
    clocks = iter(ORIGIN + timedelta(minutes=m) for m in (2, 3))
    directory = tmp_path / "frozen"
    freeze_prediction(
        directory,
        {
            "forecasts": forecasts,
            "code_proof": {
                "source_commit": "a" * 40,
                "commit_recorded_at": (ORIGIN - timedelta(hours=1)).isoformat(),
                "code_frozen_at": ORIGIN.isoformat(),
                "files": [{"path": "model.py", "sha256": "c" * 64}],
            },
        },
        model_input_as_of=ORIGIN + timedelta(minutes=1),
        input_received_at=ORIGIN,
        model_committed_at=ORIGIN - timedelta(hours=1),
        target_at=ORIGIN + timedelta(minutes=30),
        clock=lambda: next(clocks),
    )
    return directory


def fake_get(calls, include_later=False, missing=False):
    def get(url):
        calls.append(url)
        if url.endswith("/calibrations"):
            payload = {
                "city": "miami",
                "units": "celsius",
                "calibrations": [
                    {
                        "config_version": "v1",
                        "effective_at_ms": 0,
                        "published_at_ms": 0,
                        "city_reference_c": 0,
                        "stations": [
                            {"station_id": station, "weight": 0.2, "offset_c": 0}
                            for station in STATIONS
                        ],
                    }
                ],
            }
        else:
            payload = {"city": "miami", "config_version": "v1", "timeseries": []}
            if not missing:
                payload["timeseries"] = [
                    {
                        "t": int((ORIGIN + timedelta(minutes=h)).timestamp() * 1000),
                        "v": 86,
                        "status": "normal",
                        "contributors": 5,
                    }
                    for h in ((30, 60) if include_later else (30,))
                ]
        return json.dumps(payload).encode(), 200

    return get


def test_no_decision_or_not_due_makes_zero_gets(frozen, tmp_path):
    calls = []
    for directory in (frozen, tmp_path / "missing"):
        result = collector.run(
            directory,
            tmp_path / "out",
            clock=lambda: ORIGIN + timedelta(minutes=34),
            get=fake_get(calls),
        )
        assert result["requests"] == 0
    assert calls == []


def test_due_target_two_gets_exact_source_archival_and_duplicate_zero(frozen, tmp_path):
    calls = []
    output = tmp_path / "out"

    def clock():
        return ORIGIN + timedelta(minutes=35)

    result = collector.run(frozen, output, clock=clock, get=fake_get(calls))
    assert result["requests"] == len(calls) == 2
    assert "from=" in calls[0] and calls[1].endswith("/calibrations")
    assert result["rows"][0]["status"] == "SCORED"
    assert result["rows"][1]["metrics"] is None
    assert len(list(output.glob("*.final.json"))) == 1
    attempt = next(output.glob("attempt-*"))
    for name in ("collector.py", "evaluator.py", "decoder.py", "prediction.json"):
        assert (attempt / name).is_file()
    before = next(output.glob("*.final.json")).read_bytes()
    second = collector.run(frozen, output, clock=clock, get=fake_get(calls))
    assert second["requests"] == 0 and len(calls) == 2
    assert next(output.glob("*.final.json")).read_bytes() == before


def test_unexpected_later_target_not_finalized_and_global_bucket_dedup(frozen, tmp_path):
    calls = []
    output = tmp_path / "out"

    def clock():
        return ORIGIN + timedelta(minutes=65)

    report = collector.run(frozen, output, clock=clock, get=fake_get(calls, include_later=True))
    assert all(row["status"] == "SCORED" for row in report["rows"])
    assert len(list(output.glob("*.final.json"))) == 1
    repeated = collector.run(frozen, output, clock=clock, get=fake_get(calls))
    assert repeated["status"] == "ATTEMPT_ALREADY_EXISTS_NO_RETRY"
    assert len(calls) == 2


def test_missing_exact_point_does_not_complete(frozen, tmp_path):
    calls = []
    output = tmp_path / "out"
    report = collector.run(
        frozen,
        output,
        clock=lambda: ORIGIN + timedelta(minutes=35),
        get=fake_get(calls, missing=True),
    )
    assert report["rows"][0]["metrics"] is None
    assert not list(output.glob("*.final.json"))


def test_tampered_envelope_fails_before_acquisition(frozen, tmp_path):
    path = frozen / "prediction.json"
    path.write_bytes(path.read_bytes() + b" ")
    calls = []
    with pytest.raises(ValueError, match="HASH"):
        collector.run(
            frozen,
            tmp_path / "out",
            clock=lambda: ORIGIN + timedelta(minutes=35),
            get=fake_get(calls),
        )
    assert calls == []


def test_output_cannot_be_reused_with_changed_receipt(frozen, tmp_path):
    calls = []
    output = tmp_path / "out"

    def clock():
        return ORIGIN + timedelta(minutes=35)

    collector.run(frozen, output, clock=clock, get=fake_get(calls))
    raw = (frozen / "decision.json").read_bytes()
    (frozen / "decision.json").write_bytes(raw + b" ")
    assert hashlib.sha256(raw).hexdigest() != hashlib.sha256(raw + b" ").hexdigest()
    with pytest.raises(ValueError, match="COHORT_CHANGED"):
        collector.run(frozen, output, clock=clock, get=fake_get(calls))
    assert len(calls) == 2


@pytest.mark.parametrize("defect", ["copied_target", "missing_metrics", "changed_actual"])
def test_corrupt_final_cannot_skip_target_or_trigger_get(frozen, tmp_path, defect):
    calls = []
    output = tmp_path / "out"
    collector.run(frozen, output, clock=lambda: ORIGIN + timedelta(minutes=35), get=fake_get(calls))
    final = next(output.glob("*.final.json"))
    raw = final.read_bytes()
    if defect == "copied_target":
        destination = output / (
            (ORIGIN + timedelta(minutes=60)).strftime("%Y%m%dT%H%MZ") + ".final.json"
        )
        destination.write_bytes(raw)
    else:
        value = json.loads(raw)
        if defect == "missing_metrics":
            value["row"]["metrics"] = {}
        else:
            value["row"]["actual_f"] += 1
        final.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="FINAL_"):
        collector.run(
            frozen, output, clock=lambda: ORIGIN + timedelta(minutes=65), get=fake_get(calls)
        )
    assert len(calls) == 2
