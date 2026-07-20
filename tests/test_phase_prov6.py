import json
from pathlib import Path

from kalshi_predictor.phase_prov6 import write_prov6_preflight


def _prov5(path: Path) -> Path:
    path.write_text(json.dumps({"summary": {"migration_passed": True,
        "rollback_passed": True, "volume_rows_certified": 5000},
        "migration": {"seconds": 0.8},
        "backfill": {"first_run": {"rows_per_second": 2200}}}))
    return path


def test_prov6_blocks_revision_disk_reader_and_mount_gaps(tmp_path: Path) -> None:
    evidence = {"schema": {"database_bytes": 20_000, "alembic_revision": "0010",
        "required_tables_present": {"forecasts": True, "market_rankings": True,
                                    "market_snapshots": True},
        "provenance_table_present": False, "conflicting_provenance_indexes": []},
        "disk": {"available_bytes": 10_000, "external_backup_mount_ready": False},
        "writer": {"writer_count": 0, "holder_count": 2},
        "runtime": {"dual_write_enabled": False, "execution_enabled": False}}
    report = json.loads(write_prov6_preflight(
        evidence=evidence, prov5_report=_prov5(tmp_path / "p5.json"),
        output_dir=tmp_path / "out").read_text())
    assert report["summary"]["preflight_passed"] is False
    assert "alembic_parent_revision_matches" in report["summary"]["blocked_gates"]
    assert "root_disk_can_hold_verified_backup" in report["summary"]["blocked_gates"]
    assert report["migration_applied"] is False


def test_prov6_stops_for_approval_when_every_gate_passes(tmp_path: Path) -> None:
    evidence = {"schema": {"database_bytes": 20_000,
        "alembic_revision": "20260624_0011", "required_tables_present": {
            "forecasts": True, "market_rankings": True, "market_snapshots": True},
        "provenance_table_present": False, "conflicting_provenance_indexes": []},
        "disk": {"available_bytes": 30_000, "external_backup_mount_ready": True},
        "writer": {"writer_count": 0, "holder_count": 0},
        "runtime": {"dual_write_enabled": False, "execution_enabled": False}}
    report = json.loads(write_prov6_preflight(
        evidence=evidence, prov5_report=_prov5(tmp_path / "p5.json"),
        output_dir=tmp_path / "out").read_text())
    assert report["summary"]["next_action"] == "STOP_FOR_EXPLICIT_APPROVAL"
    assert report["summary"]["approval_requested"] is False
    assert report["backup_created"] is False
