from __future__ import annotations

import copy

import pytest

from scripts.local.phase4mr_review_history_compaction import (
    compact_history,
    restore_history,
    validate_compaction,
)
from tests.test_phase4mp_human_review_workflow import IDENTITY, PACKET, _workflow


def _compact(events=None, retain_tail=3):
    return compact_history(
        events if events is not None else _workflow(),
        retain_tail=retain_tail,
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )


def _validate(artifact, anchor=None):
    return validate_compaction(
        artifact,
        expected_anchor_sha256=anchor or artifact["anchor"]["anchor_sha256"],
    )


def _restore(artifact, prefix, anchor=None):
    return restore_history(
        artifact,
        prefix,
        expected_anchor_sha256=anchor or artifact["anchor"]["anchor_sha256"],
        expected_packet_sha256=PACKET,
        expected_implementation_identity_sha256=IDENTITY,
        evaluated_at="2026-08-29T01:00:00Z",
    )


def test_compaction_is_deterministic_and_retains_identity_state_and_closure() -> None:
    first = _compact()
    assert first == _compact()
    assert first["verdict"] == "PASS"
    anchor = first["anchor"]
    assert anchor["compacted_event_count"] == 7
    assert anchor["full_event_count"] == 10
    assert anchor["final_state"] == "CLOSED"
    assert anchor["review_epoch"] == 1
    assert anchor["packet_sha256"] == PACKET
    assert anchor["implementation_identity_sha256"] == IDENTITY
    assert anchor["closure_certificate_sha256"] is not None
    assert _validate(first)["verdict"] == "PASS"


@pytest.mark.parametrize("retain_tail", [0, 1, 3, 10, 25])
def test_tail_boundaries_restore_exact_history(retain_tail: int) -> None:
    events = _workflow()
    artifact = _compact(events, retain_tail)
    cut = artifact["anchor"]["compacted_event_count"]
    result = _restore(artifact, events[:cut])
    assert result["verdict"] == "PASS"
    assert result["restored_events"] == events


def test_missing_or_untrusted_anchor_fails_closed() -> None:
    artifact = _compact()
    missing = copy.deepcopy(artifact)
    missing.pop("anchor")
    assert _validate(missing, "f" * 64)["verdict"] == "REFUSE"
    assert "ANCHOR_TRUST_MISMATCH" in _validate(artifact, "f" * 64)["errors"]


@pytest.mark.parametrize(
    "mutation,error",
    [
        (lambda value: value["anchor"].update(final_state="SIGNED"), "ANCHOR_SELF_HASH_INVALID"),
        (
            lambda value: value["retained_events"][0].update(actor_id="attacker"),
            "EVENT_HASH_INVALID",
        ),
        (lambda value: value.update(retained_tail_sha256="f" * 64), "TAIL_DIGEST_INVALID"),
        (
            lambda value: value["retained_events"][0].update(previous_event_sha256="f" * 64),
            "CHAIN_LINK_INVALID",
        ),
    ],
)
def test_anchor_summary_and_tail_tampering_is_detected(mutation, error: str) -> None:
    artifact = _compact()
    trusted = artifact["anchor"]["anchor_sha256"]
    mutation(artifact)
    result = _validate(artifact, trusted)
    assert result["verdict"] == "REFUSE"
    assert any(error in item for item in result["errors"])


def test_wrong_or_missing_archived_prefix_cannot_restore() -> None:
    events = _workflow()
    artifact = _compact(events)
    cut = artifact["anchor"]["compacted_event_count"]
    assert _restore(artifact, events[: cut - 1])["verdict"] == "REFUSE"
    changed = copy.deepcopy(events[:cut])
    changed[0]["actor_id"] = "attacker"
    assert "ARCHIVED_PREFIX_DIGEST_INVALID" in _restore(artifact, changed)["errors"]


def test_repeated_compaction_of_restored_history_has_same_anchor() -> None:
    events = _workflow()
    first = _compact(events)
    cut = first["anchor"]["compacted_event_count"]
    restored = _restore(first, events[:cut])
    second = _compact(restored["restored_events"])
    assert second["anchor"] == first["anchor"]
    assert second["compaction_sha256"] == first["compaction_sha256"]


def test_invalid_source_refuses_and_all_capabilities_remain_false() -> None:
    invalid = _workflow()
    invalid[0]["actor_id"] = "attacker"
    assert _compact(invalid)["verdict"] == "REFUSE"
    safety = _compact()["safety"]
    assert safety["simulation_only"] is True
    assert all(value is False for key, value in safety.items() if key != "simulation_only")
