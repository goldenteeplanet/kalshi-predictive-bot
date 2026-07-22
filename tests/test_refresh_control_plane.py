import json
from pathlib import Path

from kalshi_predictor.refresh_control_plane import (
    build_cycle_changes,
    verify_authoritative_cloud_snapshot,
    write_authoritative_cloud_snapshot,
    write_refresh_control_plane_bundle,
)


def test_authoritative_cloud_snapshot_requires_checksum_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "cloud.json"
    snapshot = {
        "deployment_commit_sha": "a" * 40,
        "host_id": "kalshi-bot-01",
        "environment": "paper-cloud",
        "service_status": "active",
        "timer_status": "active",
        "last_successful_refresh": "2026-07-22T12:00:00+00:00",
        "collected_at": "2026-07-22T12:01:00+00:00",
        "artifact_hashes": {"gh2": "b" * 64},
    }
    write_authoritative_cloud_snapshot(snapshot, path)

    verified = verify_authoritative_cloud_snapshot(path)
    assert verified["verified"] is True
    assert verified["state"] == "VERIFIED_CLOUD"

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["snapshot"]["host_id"] = "tampered"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert verify_authoritative_cloud_snapshot(path)["verified"] is False


def test_cycle_changes_are_deterministic() -> None:
    previous = {
        "cycle_id": "one",
        "soak": {"consecutive_healthy_cycles": 3},
        "blocker_counts": {"snapshot_missing": 1},
        "candidates": [
            {
                "ticker": "KEEP",
                "lifecycle": "WARMING",
                "fresh": False,
                "blocking_gates": ["snapshot_missing"],
            },
            {"ticker": "REMOVE", "lifecycle": "RANKED"},
        ],
    }
    current = {
        "cycle_id": "two",
        "soak": {"consecutive_healthy_cycles": 0},
        "blocker_counts": {"risk_missing": 1},
        "candidates": [
            {
                "ticker": "KEEP",
                "lifecycle": "RISK_CHECKED_BLOCKED",
                "fresh": True,
                "blocking_gates": ["risk_missing"],
            },
            {"ticker": "ADD", "lifecycle": "WARMING"},
        ],
    }

    first = build_cycle_changes(previous, current)
    second = build_cycle_changes(previous, current)

    assert first == second
    assert first["candidates_added"] == ["ADD"]
    assert first["candidates_removed"] == ["REMOVE"]
    assert first["soak_reset"] is True


def test_bundle_reconciles_reports_lifecycle_quality_and_incidents(tmp_path: Path) -> None:
    output = tmp_path / "phase_gh2"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "ticker": "KXBTC-TEST",
                        "selection_tier": "MISSING_SNAPSHOT_RECOVERY",
                        "fresh": False,
                        "positive_edge": False,
                        "blocking_gates": ["snapshot_missing"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    payload = {
        "cycle_id": "gh2-test-cycle",
        "generated_at": "2026-07-22T12:00:00+00:00",
        "status": "CYCLE_NEEDS_ATTENTION",
        "soak": {"consecutive_healthy_cycles": 0, "required_healthy_cycles": 24},
        "safety": {
            "paper_order_creation_enabled": False,
            "live_execution_enabled": False,
            "autopilot_enabled": False,
        },
        "cycle_telemetry": {"stages": []},
    }

    paths = write_refresh_control_plane_bundle(
        payload, output_dir=output, candidate_manifest_path=manifest
    )

    scorecard = json.loads(Path(paths["data_quality_scorecard"]).read_text(encoding="utf-8"))
    lifecycle = json.loads(Path(paths["candidate_lifecycle"]).read_text(encoding="utf-8"))
    audit = json.loads(Path(paths["audit_evidence"]).read_text(encoding="utf-8"))
    incidents = json.loads(Path(paths["incident_history"]).read_text(encoding="utf-8"))
    assert scorecard["metrics"][0]["denominator"] == 1
    assert lifecycle["candidates"]["KXBTC-TEST"]["current_state"] == "SNAPSHOT_NEEDED"
    assert audit["scorecard"] == scorecard
    assert {row["code"] for row in incidents["incidents"]} == {
        "QUALITY_COVERAGE_BELOW_THRESHOLD",
        "UNHEALTHY_CYCLE",
    }
    assert "Paper-order creation and live execution remain disabled" in Path(
        paths["executive_report"]
    ).read_text(encoding="utf-8")
