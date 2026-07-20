import json
from pathlib import Path

from kalshi_predictor.forecast_provenance import verify_envelope, write_synthetic_provenance_audit


def test_forecast_ranking_provenance_chain_is_complete(tmp_path: Path) -> None:
    report = json.loads(write_synthetic_provenance_audit(tmp_path).read_text())
    assert report["summary"] == {"records": 3, "all_digests_valid": True, "chain_valid": True}
    weather = report["records"][1]["attribution"]
    assert weather["observation_timestamp"]
    assert weather["feature_set_id"]
    assert weather["model_version"]
    assert weather["orderbook_timestamp"]


def test_provenance_digest_detects_tampering(tmp_path: Path) -> None:
    report = json.loads(write_synthetic_provenance_audit(tmp_path).read_text())
    envelope = report["records"][0]
    envelope["attribution"]["model_version"] = "tampered"
    assert verify_envelope(envelope) is False
