from __future__ import annotations

import copy

from scripts.local.phase4mr_review_history_compaction import compact_history
from scripts.local.phase4ms_anchor_trust_rotation import (
    create_trust_store,
    detect_split_views,
    restore_with_trust,
    revoke_anchor,
    rotate_anchor,
    validate_trust_store,
)
from tests.test_phase4mp_human_review_workflow import IDENTITY, PACKET, _workflow


def _artifact(tail):
    return compact_history(
        _workflow(),
        retain_tail=tail,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )


def _chain():
    artifacts = [_artifact(value) for value in (3, 4, 5)]
    store = create_trust_store(
        artifacts[0]["anchor"]["anchor_sha256"], activated_at="2026-08-29T01:00:00Z"
    )
    store = rotate_anchor(
        store,
        artifacts[1]["anchor"]["anchor_sha256"],
        activated_at="2026-08-29T02:00:00Z",
        overlap_until="2026-08-29T02:30:00Z",
    )
    store = rotate_anchor(
        store,
        artifacts[2]["anchor"]["anchor_sha256"],
        activated_at="2026-08-29T03:00:00Z",
        overlap_until="2026-08-29T03:30:00Z",
    )
    return artifacts, store


def _validate(store, at="2026-08-29T03:15:00Z", trusted=None):
    return validate_trust_store(
        store, expected_store_sha256=trusted or store["store_sha256"], evaluated_at=at
    )


def test_chained_rotation_is_deterministic_and_overlap_is_bounded() -> None:
    artifacts, store = _chain()
    assert store == _chain()[1]
    during = _validate(store)
    assert during["verdict"] == "PASS"
    assert during["trusted_anchor_sha256"] == [
        artifacts[2]["anchor"]["anchor_sha256"],
        artifacts[1]["anchor"]["anchor_sha256"],
    ]
    after = _validate(store, "2026-08-29T04:00:00Z")
    assert after["trusted_anchor_sha256"] == [artifacts[2]["anchor"]["anchor_sha256"]]


def test_unknown_revoked_and_rolled_back_anchors_refuse() -> None:
    artifacts, store = _chain()
    assert revoke_anchor(store, "f" * 64)["verdict"] == "REFUSE"
    revoked = revoke_anchor(store, artifacts[2]["anchor"]["anchor_sha256"])
    assert _validate(revoked, trusted=revoked["store_sha256"])["trusted_anchor_sha256"] == [
        artifacts[1]["anchor"]["anchor_sha256"]
    ]
    old = create_trust_store(
        artifacts[0]["anchor"]["anchor_sha256"], activated_at="2026-08-29T01:00:00Z"
    )
    assert "STORE_TRUST_MISMATCH" in _validate(old, trusted=store["store_sha256"])["errors"]


def test_tampered_and_missing_predecessor_fail_closed() -> None:
    _, store = _chain()
    trusted = store["store_sha256"]
    tampered = copy.deepcopy(store)
    tampered["records"][1]["previous_record_sha256"] = "f" * 64
    result = _validate(tampered, trusted=trusted)
    assert result["verdict"] == "REFUSE"
    assert any("PREDECESSOR_RECORD_INVALID" in error for error in result["errors"])
    missing = copy.deepcopy(store)
    missing["records"].pop(1)
    assert _validate(missing, trusted=trusted)["verdict"] == "REFUSE"


def test_equivocation_and_same_generation_divergence_are_detected() -> None:
    artifacts, store = _chain()
    branch = rotate_anchor(
        create_trust_store(
            artifacts[0]["anchor"]["anchor_sha256"], activated_at="2026-08-29T01:00:00Z"
        ),
        "f" * 64,
        activated_at="2026-08-29T02:00:00Z",
        overlap_until="2026-08-29T02:30:00Z",
    )
    audit = detect_split_views([store, branch])
    assert audit["verdict"] == "REFUSE"
    assert "SPLIT_VIEW_OR_EQUIVOCATION" in audit["errors"]
    assert audit["reconciliation_authorized"] is False


def test_prefix_propagation_view_is_not_a_split() -> None:
    artifacts, store = _chain()
    prefix = create_trust_store(
        artifacts[0]["anchor"]["anchor_sha256"], activated_at="2026-08-29T01:00:00Z"
    )
    assert detect_split_views([prefix, store])["verdict"] == "PASS"


def test_exact_restoration_works_for_each_anchor_in_its_trust_window() -> None:
    artifacts, store = _chain()
    checks = [(0, "2026-08-29T01:30:00Z"), (1, "2026-08-29T02:15:00Z"), (2, "2026-08-29T03:15:00Z")]
    events = _workflow()
    for index, at in checks:
        artifact = artifacts[index]
        cut = artifact["anchor"]["compacted_event_count"]
        result = restore_with_trust(
            artifact,
            events[:cut],
            store,
            expected_store_sha256=store["store_sha256"],
            expected_packet_sha256=PACKET,
            expected_implementation_identity_sha256=IDENTITY,
            evaluated_at=at,
        )
        assert result["verdict"] == "PASS"
        assert result["restored_events"] == events


def test_expired_predecessor_and_unknown_artifact_cannot_restore() -> None:
    artifacts, store = _chain()
    events = _workflow()
    old = artifacts[0]
    cut = old["anchor"]["compacted_event_count"]
    refused = restore_with_trust(
        old,
        events[:cut],
        store,
        expected_store_sha256=store["store_sha256"],
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T04:00:00Z",
    )
    assert refused["verdict"] == "REFUSE"
    unknown = copy.deepcopy(artifacts[2])
    unknown["anchor"]["anchor_sha256"] = "f" * 64
    assert (
        restore_with_trust(
            unknown,
            events[:5],
            store,
            expected_store_sha256=store["store_sha256"],
            expected_packet_sha256=PACKET,
            expected_implementation_identity_sha256=IDENTITY,
            evaluated_at="2026-08-29T03:15:00Z",
        )["verdict"]
        == "REFUSE"
    )


def test_no_operational_capabilities_exist() -> None:
    _, store = _chain()
    safety = _validate(store)["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
