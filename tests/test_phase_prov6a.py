import json
from pathlib import Path

from kalshi_predictor.phase_prov6a import EXPECTED_COLUMNS, write_prov6a_preview


def test_prov6a_classifies_matching_schema_with_stale_marker(tmp_path: Path) -> None:
    evidence = {"lineage": {"market_legs_present": True,
        "market_legs_columns": [{"name": name} for name in EXPECTED_COLUMNS],
        "market_legs_indexes": [
            {"name": "ix_market_legs_category_ticker", "columns": ["category", "ticker"],
             "unique": 0},
            {"name": "sqlite_autoindex_market_legs_1", "columns": ["ticker", "leg_index"],
             "unique": 1}],
        "alembic_revision": "20260623_0010", "revision_file_present": True,
        "revision_file_hash_matched": True},
        "storage": {"external_device_attached": False, "backup_mount_ready": False,
                    "backup_available_bytes": 0, "minimum_required_bytes": 25_000,
                    "block_devices": ["vda"]},
        "writer": {"writer_count": 0, "holder_count": 2}}
    report = json.loads(write_prov6a_preview(
        evidence=evidence, output_dir=tmp_path).read_text())
    assert report["lineage_assessment"]["classification"] == "SCHEMA_PRESENT_MARKER_STALE"
    assert report["summary"]["schema_0011_present"] is True
    assert report["alembic_stamped_or_upgraded"] is False
    assert report["summary"]["preflight_ready_for_approval"] is False


def test_prov6a_never_authorizes_mount_or_lineage_write(tmp_path: Path) -> None:
    evidence = {"lineage": {"market_legs_present": True,
        "market_legs_columns": [{"name": name} for name in EXPECTED_COLUMNS],
        "market_legs_indexes": [
            {"name": "ix_market_legs_category_ticker", "columns": ["category", "ticker"],
             "unique": 0}, {"name": "u", "columns": ["ticker", "leg_index"], "unique": 1}],
        "alembic_revision": "20260624_0011", "revision_file_present": True,
        "revision_file_hash_matched": True},
        "storage": {"external_device_attached": True, "backup_mount_ready": True,
                    "backup_available_bytes": 30_000, "minimum_required_bytes": 25_000,
                    "block_devices": ["vda", "sda"]},
        "writer": {"writer_count": 0, "holder_count": 0}}
    report = json.loads(write_prov6a_preview(
        evidence=evidence, output_dir=tmp_path).read_text())
    assert report["summary"]["preflight_ready_for_approval"] is True
    assert report["reconciliation_plan"]["apply_authorized"] is False
    assert report["volume_attached_or_mounted"] is False
