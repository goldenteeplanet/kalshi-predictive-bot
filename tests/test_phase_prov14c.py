from __future__ import annotations

import json
from pathlib import Path

from kalshi_predictor.phase_prov14c import build_prov14c_stability_census


def _report(path: Path, boundary: int, *, broken: bool = False) -> Path:
    rows = []
    for offset, model in enumerate(("crypto_v2", "weather_v2"), start=1):
        rows.append(
            {
                "event_id": boundary + offset,
                "model_name": model,
                "passed": not broken,
                "failures": ["BROKEN"] if broken else [],
                "source_observation_ref": {"table": "source", "id": offset},
                "market_snapshot_id": boundary + 100 + offset,
                "feature_source_table": "features",
                "feature_source_id": boundary + 200 + offset,
            }
        )
    payload = {
        "phase": "PROV-14",
        "boundary": {"after_event_id": boundary, "limit": 200},
        "summary": {"certification_passed": not broken},
        "guardrails": {"execution_enabled": False},
        "rows": rows,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_three_distinct_exact_cycles_pass(tmp_path: Path) -> None:
    paths = [_report(tmp_path / f"cycle-{index}.json", index * 10) for index in range(3)]

    report = build_prov14c_stability_census(paths)

    assert report["summary"]["stability_census_passed"] is True
    assert report["summary"]["events"] == 6


def test_duplicate_boundary_fails_closed(tmp_path: Path) -> None:
    paths = [_report(tmp_path / f"cycle-{index}.json", 10) for index in range(3)]

    report = build_prov14c_stability_census(paths)

    assert report["summary"]["stability_census_passed"] is False
    assert "BOUNDARY_NOT_DISTINCT" in report["cycles"][1]["failures"]


def test_failed_attribution_cycle_fails_closed(tmp_path: Path) -> None:
    paths = [
        _report(tmp_path / "cycle-1.json", 10),
        _report(tmp_path / "cycle-2.json", 20, broken=True),
        _report(tmp_path / "cycle-3.json", 30),
    ]

    report = build_prov14c_stability_census(paths)

    assert report["summary"]["stability_census_passed"] is False
    assert "EVENT_ATTRIBUTION_FAILED" in report["cycles"][1]["failures"]


def test_synthetic_preview_never_claims_runtime_certification(tmp_path: Path) -> None:
    paths = [_report(tmp_path / f"cycle-{index}.json", index * 10) for index in range(3)]

    report = build_prov14c_stability_census(paths, synthetic_preview=True)

    assert report["summary"]["stability_census_passed"] is True
    assert report["summary"]["runtime_stability_certified"] is False
    assert report["evidence_kind"] == "synthetic_fixture"


def test_boundaries_must_strictly_increase(tmp_path: Path) -> None:
    paths = [
        _report(tmp_path / "cycle-1.json", 20),
        _report(tmp_path / "cycle-2.json", 10),
        _report(tmp_path / "cycle-3.json", 30),
    ]
    report = build_prov14c_stability_census(paths)
    assert report["summary"]["stability_census_passed"] is False
    assert "BOUNDARY_NOT_STRICTLY_INCREASING" in report["cycles"][1]["failures"]


def test_unexpected_model_is_rejected(tmp_path: Path) -> None:
    paths = [_report(tmp_path / f"cycle-{index}.json", index * 10) for index in range(3)]
    payload = json.loads(paths[1].read_text())
    payload["rows"].append({
        "event_id": 29, "model_name": "market_implied_v1", "passed": True,
        "failures": [], "source_observation_ref": {"table": "source", "id": 9},
        "market_snapshot_id": 9, "feature_source_table": "features", "feature_source_id": 9,
    })
    paths[1].write_text(json.dumps(payload))
    report = build_prov14c_stability_census(paths)
    assert report["summary"]["stability_census_passed"] is False
    assert "UNEXPECTED_MODEL:market_implied_v1" in report["cycles"][1]["failures"]


def test_duplicate_event_within_cycle_is_diagnosed(tmp_path: Path) -> None:
    paths = [_report(tmp_path / f"cycle-{index}.json", index * 10) for index in range(3)]
    payload = json.loads(paths[1].read_text())
    payload["rows"].append(dict(payload["rows"][0]))
    paths[1].write_text(json.dumps(payload))
    report = build_prov14c_stability_census(paths)
    assert report["summary"]["stability_census_passed"] is False
    assert "EVENT_ID_DUPLICATE_WITHIN_CYCLE" in report["cycles"][1]["failures"]
