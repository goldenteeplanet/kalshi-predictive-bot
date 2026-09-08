from dataclasses import replace

import pytest

from kalshi_predictor.workstation.process_tree_evidence_capture import (
    ProcessTreeEvidenceCaptureError,
    capture_process_tree_evidence,
    make_process_node_evidence,
    validate_process_tree_capture,
)


def _node(n, parent=None, **overrides):
    fields = dict(
        process_id_hash=str(n) * 64,
        parent_process_id_hash=None if parent is None else str(parent) * 64,
        executable_hash="a" * 64,
        state="RUNNING",
        started_at_epoch_seconds=100,
        complete=True,
    )
    fields.update(overrides)
    return make_process_node_evidence(**fields)


def test_tree_is_deterministic_redacted_and_non_authorizing() -> None:
    nodes = [_node(1), _node(2, 1), _node(3, 2)]
    first = capture_process_tree_evidence(list(reversed(nodes)), captured_at_epoch_seconds=100)
    second = capture_process_tree_evidence(nodes, captured_at_epoch_seconds=100)
    assert first == second and first.status == "CAPTURED" and first.maximum_observed_depth == 2
    assert (
        first.identifiers_redacted
        and not first.command_lines_retained
        and not first.environment_retained
    )
    assert not any(
        (
            first.recovery_authorized,
            first.service_control_authorized,
            first.host_restart_authorized,
            first.execution_authorized,
        )
    )
    validate_process_tree_capture(first)


def test_exact_node_and_depth_bounds_pass_then_excess_refuses() -> None:
    nodes = [_node(1), _node(2, 1), _node(3, 2)]
    assert (
        capture_process_tree_evidence(
            nodes, captured_at_epoch_seconds=100, max_nodes=3, max_depth=2
        ).status
        == "CAPTURED"
    )
    assert (
        capture_process_tree_evidence(nodes, captured_at_epoch_seconds=100, max_depth=1).status
        == "REFUSED"
    )
    with pytest.raises(ProcessTreeEvidenceCaptureError, match="NODE_BOUND_EXCEEDED"):
        capture_process_tree_evidence(nodes, captured_at_epoch_seconds=100, max_nodes=2)


def test_missing_parent_incomplete_future_duplicate_and_cycle_fail_closed() -> None:
    assert (
        capture_process_tree_evidence([_node(2, 9)], captured_at_epoch_seconds=100).status
        == "PARTIAL"
    )
    assert (
        capture_process_tree_evidence(
            [_node(1, complete=False)], captured_at_epoch_seconds=100
        ).status
        == "PARTIAL"
    )
    assert (
        capture_process_tree_evidence(
            [_node(1, started_at_epoch_seconds=101)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )
    assert (
        capture_process_tree_evidence([_node(1), _node(1)], captured_at_epoch_seconds=100).status
        == "TAMPERED"
    )
    assert (
        capture_process_tree_evidence(
            [_node(1, 2), _node(2, 1)], captured_at_epoch_seconds=100
        ).status
        == "TAMPERED"
    )


def test_node_capture_safety_tampering_and_process_surfaces_fail_closed() -> None:
    with pytest.raises(ProcessTreeEvidenceCaptureError, match="NODE_HASH_MISMATCH"):
        capture_process_tree_evidence(
            [replace(_node(1), state="ZOMBIE")], captured_at_epoch_seconds=100
        )
    result = capture_process_tree_evidence([_node(1)], captured_at_epoch_seconds=100)
    with pytest.raises(ProcessTreeEvidenceCaptureError, match="CAPTURE_HASH_MISMATCH"):
        validate_process_tree_capture(replace(result, reasons=("FORGED",)))
    with pytest.raises(ProcessTreeEvidenceCaptureError, match="SAFETY_BOUNDARY"):
        validate_process_tree_capture(replace(result, command_lines_retained=True))
    forbidden = {
        "psutil",
        "process_iter",
        "open",
        "run",
        "popen",
        "subprocess",
        "kill",
        "terminate",
        "restart",
        "execute",
        "order",
        "database",
    }
    assert forbidden.isdisjoint(capture_process_tree_evidence.__code__.co_names)
