import json
from datetime import UTC, datetime
from pathlib import Path

from kalshi_predictor.roadmap.artifacts import (
    verify_signed_artifact,
    write_category_certification,
)
from kalshi_predictor.roadmap.category_contract import CategoryPipelineEvidence


def test_category_artifact_is_atomic_checksummed_and_tamper_evident(tmp_path: Path) -> None:
    path = write_category_certification(
        CategoryPipelineEvidence(
            category="sports",
            generated_at=datetime.now(UTC).isoformat(),
            source_state="BLOCKED",
            source_name="manual",
            synthetic_or_manual_only=True,
        ),
        reports_root=tmp_path,
    )
    verified = verify_signed_artifact(path)
    assert verified["verified"] is True
    assert verified["payload"]["live_v1_scope_certified"] is False

    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["live_v1_scope_certified"] = True
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert verify_signed_artifact(path)["verified"] is False
