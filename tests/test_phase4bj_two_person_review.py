from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts/local/phase4bj_two_person_review.py"
    spec = importlib.util.spec_from_file_location("phase4bj_tested", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _fixture(tmp_path: Path):
    module = _module()
    tmp_path.mkdir(parents=True, exist_ok=True)
    row_hashes = ["a" * 64, "b" * 64]
    target = {
        "schema": module.TARGET_SCHEMA,
        "row_hashes": row_hashes,
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
    }
    target["artifact_hash"] = module._hash(target)
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps(target))
    rows = []
    for index, row_hash in enumerate(row_hashes):
        rows.append(
            {
                "row_hash": row_hash,
                "reviewer_one": {
                    "identity": "reviewer-one",
                    "reviewed_at": (NOW - timedelta(minutes=2, seconds=index)).isoformat(),
                    "decision": "APPROVE",
                },
                "reviewer_two": {
                    "identity": "reviewer-two",
                    "reviewed_at": (NOW - timedelta(minutes=1, seconds=index)).isoformat(),
                    "decision": "APPROVE",
                },
            }
        )
    approval = {
        "schema": module.APPROVAL_SCHEMA,
        "externally_supplied": True,
        "generated_by_guarded_tooling": False,
        "target_hash": target["artifact_hash"],
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "revoked": False,
        "rows": rows,
    }
    approval["artifact_hash"] = module._hash(approval)
    approval_path = tmp_path / "approval.json"
    approval_path.write_text(json.dumps(approval))
    return module, target_path, approval_path


def _mutate(module, path: Path, mutate, *, rehash=True):
    payload = json.loads(path.read_text())
    mutate(payload)
    if rehash:
        payload["artifact_hash"] = module._hash(payload)
    path.write_text(json.dumps(payload))


def _reasons(module, target, approval, now=NOW):
    return set(module.build(target, approval, now=now)[0]["reason_codes"])


def test_distinct_independent_exactly_bound_reviews_pass(tmp_path: Path):
    module, target, approval = _fixture(tmp_path)
    validation, refusal = module.build(target, approval, now=NOW)
    assert validation["two_person_review_valid"] is True
    assert validation["shared_expiration"] == (NOW + timedelta(minutes=5)).isoformat()
    assert validation["approval_generated"] is False
    assert refusal["refusal_required"] is False
    assert validation["artifact_hash"] == module._hash(validation)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda p: p["rows"][0]["reviewer_two"].update(identity="reviewer-one"),
            "DUPLICATE_OR_SELF_REVIEW",
        ),
        (
            lambda p: p["rows"][0]["reviewer_two"].update(
                reviewed_at=p["rows"][0]["reviewer_one"]["reviewed_at"]
            ),
            "REVIEW_TIMESTAMPS_NOT_SEPARATE",
        ),
        (
            lambda p: p["rows"][0]["reviewer_two"].update(decision="REJECT"),
            "INDEPENDENT_ROW_APPROVAL_MISSING",
        ),
        (lambda p: p["rows"].pop(), "ROW_BINDING_OR_DUPLICATION_INVALID"),
        (
            lambda p: p["rows"].append(dict(p["rows"][0])),
            "ROW_BINDING_OR_DUPLICATION_INVALID",
        ),
        (lambda p: p.update(target_hash="0" * 64), "TARGET_BINDING_MISMATCH"),
        (lambda p: p.update(revoked=True), "APPROVAL_REVOKED"),
        (lambda p: p.update(externally_supplied=False), "APPROVAL_PROVENANCE_INVALID"),
    ],
)
def test_identity_timestamp_decision_rows_binding_revocation_and_provenance_refuse(
    tmp_path: Path, mutation, reason: str
):
    module, target, approval = _fixture(tmp_path)
    _mutate(module, approval, mutation)
    assert reason in _reasons(module, target, approval)


def test_shared_expiration_exact_boundary_and_future_review_refuse(tmp_path: Path):
    module, target, approval = _fixture(tmp_path / "expiration")
    assert "SHARED_EXPIRATION_REACHED" not in _reasons(
        module, target, approval, NOW + timedelta(minutes=5) - timedelta(microseconds=1)
    )
    assert "SHARED_EXPIRATION_REACHED" in _reasons(
        module, target, approval, NOW + timedelta(minutes=5)
    )
    module, target, approval = _fixture(tmp_path / "future")
    _mutate(
        module,
        approval,
        lambda p: p["rows"][0]["reviewer_one"].update(
            reviewed_at=(NOW + timedelta(seconds=1)).isoformat()
        ),
    )
    assert "REVIEW_TIMESTAMP_OUTSIDE_WINDOW" in _reasons(module, target, approval)


def test_artifact_tampering_naive_time_and_static_validation_only(tmp_path: Path):
    module, target, approval = _fixture(tmp_path / "tamper")
    _mutate(module, approval, lambda p: p.update(extra=True), rehash=False)
    with pytest.raises(ValueError, match="APPROVAL_SCHEMA_OR_HASH_INVALID"):
        module.build(target, approval, now=NOW)
    module, target, approval = _fixture(tmp_path / "time")
    with pytest.raises(ValueError, match="EVALUATION_TIMEZONE_MISSING"):
        module.build(target, approval, now=datetime(2026, 8, 25, 12, 0))
    source = (Path(__file__).parents[1] / "scripts/local/phase4bj_two_person_review.py").read_text()
    assert 'approval_generated": False' in source
    assert "sqlite3" not in source
    assert "--production-db" not in source
