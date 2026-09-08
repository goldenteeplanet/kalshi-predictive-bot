from __future__ import annotations

import copy
import hashlib
import json

import pytest

from scripts.local.phase4om_recovery_tail_soak import run_soak
from scripts.local.phase4on_tail_regression_gate import create_baseline
from scripts.local.phase4oo_baseline_lineage import (
    append_baseline,
    detect_forks,
    propose_rollback,
    verify_history,
)


def _history():
    first = create_baseline(run_soak(seed=3), version=1)
    second = create_baseline(run_soak(seed=7), version=2)
    history = append_baseline([], first, promotion_comparison_sha256=None)
    return append_baseline(history, second, promotion_comparison_sha256="c" * 64)


def test_hash_linked_multi_version_history_passes_and_retains_all_versions() -> None:
    history = _history()
    result = verify_history(
        history, trusted_head_sha256=history[-1]["entry_sha256"], minimum_retained=2
    )
    assert result["verdict"] == "PASS"
    assert result["versions"] == [1, 2]
    assert result["retained_count"] == 2


def test_rollback_is_frozen_nonclaiming_and_does_not_rewrite_history() -> None:
    history = _history()
    original = copy.deepcopy(history)
    rollback = propose_rollback(
        history,
        trusted_head_sha256=history[-1]["entry_sha256"],
        target_version=1,
        reason="tail regression investigation",
    )
    assert rollback["verdict"] == "PASS"
    assert rollback["mode"] == "FROZEN_OPERATIONAL_ROLLBACK"
    assert rollback["performance_claim"] is False
    assert rollback["capabilities_allowed"] is False
    assert rollback["head_rewritten"] is False
    assert history == original


def test_deletion_rewrite_gap_stale_head_and_missing_promotion_refuse() -> None:
    history = _history()
    cases = []
    rewritten = copy.deepcopy(history)
    rewritten[0]["baseline_sha256"] = "f" * 64
    cases.append((rewritten, history[-1]["entry_sha256"], 2, "LINEAGE_ENTRY_HASH_MISMATCH"))
    gap = copy.deepcopy(history)
    gap[1]["version"] = 3
    cases.append((gap, history[-1]["entry_sha256"], 2, "VERSION_GAP_OR_ROLLBACK"))
    missing = copy.deepcopy(history)
    missing[1]["promotion_comparison_sha256"] = None
    cases.append((missing, history[-1]["entry_sha256"], 2, "PROMOTION_EVIDENCE_INVALID"))
    cases.append((history[:1], history[-1]["entry_sha256"], 2, "TRUSTED_HEAD_MISMATCH"))
    for candidate, head, retained, expected in cases:
        assert (
            expected
            in verify_history(candidate, trusted_head_sha256=head, minimum_retained=retained)[
                "errors"
            ]
        )


def test_append_requires_exact_version_and_verified_promotion_reference() -> None:
    first = create_baseline(run_soak(seed=3), version=1)
    history = append_baseline([], first, promotion_comparison_sha256=None)
    wrong = create_baseline(run_soak(seed=7), version=3)
    with pytest.raises(ValueError, match="append exactly"):
        append_baseline(history, wrong, promotion_comparison_sha256="c" * 64)
    second = create_baseline(run_soak(seed=7), version=2)
    with pytest.raises(ValueError, match="required"):
        append_baseline(history, second, promotion_comparison_sha256=None)


def test_competing_children_are_detected_as_fork() -> None:
    history = _history()
    alternate = copy.deepcopy(history)
    alternate[1]["baseline_sha256"] = "e" * 64
    unsigned = {key: value for key, value in alternate[1].items() if key != "entry_sha256"}
    alternate[1]["entry_sha256"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    result = detect_forks([history, alternate])
    assert result["verdict"] == "REFUSE"
    assert "LINEAGE_FORK_DETECTED" in result["errors"]


def test_invalid_rollback_target_or_unverified_head_refuses() -> None:
    history = _history()
    assert (
        propose_rollback(
            history,
            trusted_head_sha256=history[-1]["entry_sha256"],
            target_version=2,
            reason="not older",
        )["verdict"]
        == "REFUSE"
    )
    result = propose_rollback(
        history, trusted_head_sha256="f" * 64, target_version=1, reason="stale"
    )
    assert "LINEAGE_NOT_VERIFIED" in result["errors"]


def test_verification_is_deterministic_input_preserving_and_execution_free() -> None:
    history = _history()
    original = copy.deepcopy(history)
    kwargs = {"trusted_head_sha256": history[-1]["entry_sha256"], "minimum_retained": 2}
    first = verify_history(history, **kwargs)
    assert first == verify_history(history, **kwargs)
    assert history == original
    assert first["safety"]["offline_only"] is True
    assert all(value is False for key, value in first["safety"].items() if key != "offline_only")
