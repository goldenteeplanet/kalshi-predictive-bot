import json
from pathlib import Path

from kalshi_predictor.phase_prov5 import write_prov5_certification


def _prov3(path: Path) -> Path:
    path.write_text(json.dumps({"rows": [
        {"forecast_id": 1, "ticker": "BTC", "model_name": "crypto_v2",
         "forecasted_at": "2026-07-16 16:00:00",
         "feature_mapping": {"resolved": True, "source_table": "crypto_features",
                             "source_id": 10}},
        {"forecast_id": 2, "ticker": "NYC", "model_name": "weather_v2",
         "forecasted_at": "2026-07-16 16:00:00",
         "feature_mapping": {"resolved": True, "source_table": "weather_features",
                             "source_id": 20}},
        {"forecast_id": 3, "ticker": "SPORT", "model_name": "sports_v1",
         "forecasted_at": "2026-07-16 16:00:00",
         "feature_mapping": {"resolved": True, "source_table": "sports_features",
                             "source_id": 30}},
    ]}))
    return path


def test_prov5_backfill_is_idempotent_and_clone_is_disposed(tmp_path: Path) -> None:
    report = json.loads(write_prov5_certification(
        prov3_report=_prov3(tmp_path / "prov3.json"), output_dir=tmp_path / "out",
        volume_rows=250,
    ).read_text())
    assert report["backfill"]["event_count_after_first"] == 250
    assert report["backfill"]["second_run"]["inserted"] == 0
    assert report["backfill"]["idempotent"] is True
    assert report["backfill"]["hash_chain_valid"] is True
    assert report["clone"]["disposed"] is True


def test_prov5_rollback_preserves_legacy_data_and_forbids_cloud_apply(tmp_path: Path) -> None:
    report = json.loads(write_prov5_certification(
        prov3_report=_prov3(tmp_path / "prov3.json"), output_dir=tmp_path / "out",
        volume_rows=100,
    ).read_text())
    assert report["rollback"]["provenance_table_removed"] is True
    assert report["rollback"]["legacy_data_unchanged"] is True
    assert report["deployment_plan"]["apply_authorized"] is False
    assert report["summary"]["ready_for_cloud_apply"] is False
    assert report["cloud_database_writes"] == 0
