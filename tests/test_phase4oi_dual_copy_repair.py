from __future__ import annotations

import copy

from scripts.local.phase4oh_freeze_durability import append_event, create_checkpoint
from scripts.local.phase4oi_dual_copy_repair import create_anchor, create_copy, reconcile_copies


def _journal():
    return append_event([], event_type="FREEZE", evidence_sha256="a" * 64, evidence_verdict="PASS")


def test_equal_valid_copies_choose_canonical_frozen_state() -> None:
    journal = _journal()
    anchor = create_anchor(journal, generation=1)
    result = reconcile_copies(
        [create_copy("a", journal, None), create_copy("b", journal, None)], anchor
    )
    assert result["verdict"] == "PASS"
    assert result["state"] == "FROZEN"
    assert result["capabilities_allowed"] is False


def test_unambiguous_descendant_wins_and_repairs_stale_copy() -> None:
    short = _journal()
    long = append_event(
        short, event_type="FREEZE", evidence_sha256="b" * 64, evidence_verdict="PASS"
    )
    anchor = create_anchor(short, generation=1)
    result = reconcile_copies(
        [create_copy("old", short, None), create_copy("new", long, create_checkpoint(long))],
        anchor,
    )
    assert result["verdict"] == "PASS"
    assert result["canonical_journal_head"] == long[-1]["event_sha256"]
    assert [row["target_copy_id"] for row in result["repair_plan"]] == ["old"]


def test_single_corrupt_copy_is_repaired_from_valid_frozen_copy() -> None:
    journal = _journal()
    anchor = create_anchor(journal, generation=1)
    good = create_copy("good", journal, None)
    bad = copy.deepcopy(create_copy("bad", journal, None))
    bad["journal"][0]["evidence_sha256"] = "f" * 64
    result = reconcile_copies([bad, good], anchor)
    assert result["verdict"] == "PASS"
    assert result["invalid_copy_ids"] == ["bad"]
    assert result["state"] == "FROZEN"
    assert result["repair_plan"][0]["replacement_journal"] == journal


def test_divergent_valid_histories_fail_closed() -> None:
    base = _journal()
    left = append_event(
        base, event_type="FREEZE", evidence_sha256="b" * 64, evidence_verdict="PASS"
    )
    right = append_event(
        base, event_type="FREEZE", evidence_sha256="c" * 64, evidence_verdict="PASS"
    )
    result = reconcile_copies(
        [create_copy("left", left, None), create_copy("right", right, None)],
        create_anchor(base, generation=1),
    )
    assert result["verdict"] == "REFUSE"
    assert result["state"] == "FROZEN"
    assert "DIVERGENT_VALID_HISTORIES" in result["errors"]


def test_stale_anchor_rollback_and_both_corrupt_fail_closed() -> None:
    journal = _journal()
    anchor = create_anchor(journal, generation=1)
    ahead = append_event(
        journal, event_type="FREEZE", evidence_sha256="b" * 64, evidence_verdict="PASS"
    )
    newer_anchor = create_anchor(ahead, generation=2)
    rollback = reconcile_copies([create_copy("a", journal, None)], newer_anchor)
    assert "COPY_BEHIND_ANCHOR" in rollback["invalid_copy_ids"] or rollback["verdict"] == "REFUSE"
    forged_anchor = copy.deepcopy(anchor)
    forged_anchor["generation"] = 2
    assert reconcile_copies([create_copy("a", ahead, None)], forged_anchor)["verdict"] == "REFUSE"
    bad_a = create_copy("a", journal, None)
    bad_b = create_copy("b", journal, None)
    bad_a["copy_sha256"] = "1" * 64
    bad_b["copy_sha256"] = "2" * 64
    result = reconcile_copies([bad_a, bad_b], anchor)
    assert result["verdict"] == "REFUSE"
    assert result["state"] == "FROZEN"


def test_reconciliation_is_order_independent_input_preserving_and_safe() -> None:
    journal = _journal()
    anchor = create_anchor(journal, generation=1)
    copies = [create_copy("b", journal, None), create_copy("a", journal, None)]
    original = copy.deepcopy((copies, anchor))
    forward = reconcile_copies(copies, anchor)
    reverse = reconcile_copies(list(reversed(copies)), anchor)
    assert forward == reverse
    assert (copies, anchor) == original
    assert forward["safety"]["offline_only"] is True
    assert all(value is False for key, value in forward["safety"].items() if key != "offline_only")
