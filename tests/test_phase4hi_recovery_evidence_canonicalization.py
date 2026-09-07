from __future__ import annotations

from dataclasses import replace

import pytest
from kalshi_predictor.workstation.authoritative_scheduler_health import (
    AUTHORITATIVE_UNIT,
    make_scheduler_health_observation,
    probe_authoritative_scheduler_health,
)
from kalshi_predictor.workstation.keepalive_gap_classifier import (
    classify_keepalive_gaps,
    make_keepalive_sample,
)
from kalshi_predictor.workstation.protected_invariant_recovery_gate import (
    evaluate_protected_invariant_recovery_gate,
    make_protected_invariant_observation,
)
from kalshi_predictor.workstation.recovery_evidence_canonicalization import (
    PHASE_ORDER,
    RecoveryEvidenceCanonicalizationError,
    canonicalize_recovery_evidence,
    validate_canonical_recovery_evidence_bundle,
)
from kalshi_predictor.workstation.user_systemd_reachability import (
    make_user_systemd_probe_observation,
    probe_user_systemd_reachability,
)
from kalshi_predictor.workstation.writer_exclusivity_recovery_gate import (
    evaluate_writer_exclusivity_recovery_gate,
    make_writer_inventory_observation,
)
from kalshi_predictor.workstation.wsl_boot_identity_monitor import (
    make_wsl_boot_identity_observation,
    monitor_wsl_boot_identity,
)
from kalshi_predictor.workstation.wsl_liveness_evidence import (
    collect_wsl_liveness_evidence,
    make_wsl_liveness_probe_result,
)


def test_complete_chain_is_canonical_deterministic_and_read_only() -> None:
    inputs = _inputs()
    first = canonicalize_recovery_evidence(**inputs)
    second = canonicalize_recovery_evidence(**inputs)
    validate_canonical_recovery_evidence_bundle(first)
    assert first.status == "READY"
    assert first.phase_order == PHASE_ORDER
    assert tuple(entry.phase for entry in first.entries) == PHASE_ORDER
    assert first.bundle_hash == second.bundle_hash
    assert first.recovery_authorized is False


def test_nonpassing_upstream_status_is_denied_with_phase_identity() -> None:
    inputs = _inputs(keepalive_gap_seconds=61)
    bundle = canonicalize_recovery_evidence(**inputs)
    assert bundle.status == "DENIED"
    assert bundle.reasons == ("RECOVERY_EVIDENCE_STATUS_DENIED:4HD:GAP_DETECTED",)
    assert bundle.alert_required is True


def test_exact_freshness_boundary_passes_and_older_is_stale() -> None:
    assert canonicalize_recovery_evidence(**_inputs(age=120)).status == "READY"
    assert canonicalize_recovery_evidence(**_inputs(age=121)).status == "STALE"
    with pytest.raises(RecoveryEvidenceCanonicalizationError, match="BUNDLE_BOUND_INVALID"):
        canonicalize_recovery_evidence(**_inputs(), max_evidence_age_seconds=True)


def test_incomplete_upstream_evidence_fails_closed() -> None:
    inputs = _inputs(liveness_complete=False)
    bundle = canonicalize_recovery_evidence(**inputs)
    assert bundle.status == "INCOMPLETE"
    assert "RECOVERY_EVIDENCE_INCOMPLETE:4HC" in bundle.reasons
    assert bundle.prerequisites_ready is False


def test_wrong_type_and_tampered_upstream_evidence_fail_closed() -> None:
    inputs = _inputs()
    with pytest.raises(RecoveryEvidenceCanonicalizationError, match="EVIDENCE_TYPE_INVALID:4HB"):
        canonicalize_recovery_evidence(**{**inputs, "boot_identity": object()})
    tampered = replace(inputs["scheduler_health"], evidence_hash="0" * 64)
    with pytest.raises(RecoveryEvidenceCanonicalizationError, match="EVIDENCE_INVALID:4HF"):
        canonicalize_recovery_evidence(**{**inputs, "scheduler_health": tampered})


def test_bundle_entry_order_hash_and_safety_tampering_fail_closed() -> None:
    bundle = canonicalize_recovery_evidence(**_inputs())
    with pytest.raises(RecoveryEvidenceCanonicalizationError, match="BUNDLE_PHASE_ORDER_INVALID"):
        validate_canonical_recovery_evidence_bundle(
            replace(bundle, entries=tuple(reversed(bundle.entries)))
        )
    with pytest.raises(RecoveryEvidenceCanonicalizationError, match="BUNDLE_ENTRIES_HASH_MISMATCH"):
        changed_entry = replace(bundle.entries[0], evidence_hash="0" * 64)
        validate_canonical_recovery_evidence_bundle(
            replace(bundle, entries=(changed_entry, *bundle.entries[1:]))
        )
    with pytest.raises(
        RecoveryEvidenceCanonicalizationError, match="BUNDLE_SAFETY_BOUNDARY_INVALID"
    ):
        validate_canonical_recovery_evidence_bundle(replace(bundle, recovery_authorized=True))


def test_canonicalizer_has_no_query_control_notification_or_mutation_surface() -> None:
    names = set(canonicalize_recovery_evidence.__code__.co_names)
    assert names.isdisjoint(
        {
            "Popen",
            "commit",
            "connect",
            "execute",
            "open",
            "restart",
            "shutdown",
            "start",
            "stop",
            "systemctl",
            "toast",
            "write",
        }
    )


def _inputs(*, age=1, keepalive_gap_seconds=30, liveness_complete=True):
    source = "a" * 64
    boot = monitor_wsl_boot_identity(
        [
            make_wsl_boot_identity_observation(
                sequence=index,
                observed_at_epoch_seconds=100 + index,
                boot_identity="11111111-1111-4111-8111-111111111111",
                complete=True,
                source_identity_hash=source,
                evidence_age_seconds=age,
            )
            for index in (1, 2)
        ]
    )
    liveness = collect_wsl_liveness_evidence(
        make_wsl_liveness_probe_result(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            duration_milliseconds=25,
            exit_code=0,
            wsl_available=True,
            distribution_running=True,
            complete=liveness_complete,
            probe_name="wsl-status-v1",
            source_identity_hash=source,
            output="running",
        )
    )
    keepalive = classify_keepalive_gaps(
        [
            make_keepalive_sample(
                sequence=1,
                observed_at_epoch_seconds=100,
                present=True,
                complete=True,
                evidence_age_seconds=age,
                source_identity_hash=source,
            ),
            make_keepalive_sample(
                sequence=2,
                observed_at_epoch_seconds=100 + keepalive_gap_seconds,
                present=True,
                complete=True,
                evidence_age_seconds=age,
                source_identity_hash=source,
            ),
        ]
    )
    systemd = probe_user_systemd_reachability(
        make_user_systemd_probe_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            duration_milliseconds=25,
            exit_code=0,
            manager_reachable=True,
            runtime_directory_present=True,
            dbus_session_present=True,
            complete=True,
            probe_name="systemctl-user-is-system-running-v1",
            source_identity_hash=source,
            output="running",
        )
    )
    scheduler = probe_authoritative_scheduler_health(
        make_scheduler_health_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            duration_milliseconds=25,
            unit_name=AUTHORITATIVE_UNIT,
            load_state="loaded",
            active_state="active",
            sub_state="running",
            main_pid=123,
            observed_writer_count=1,
            complete=True,
            probe_name="systemctl-user-show-v1",
            source_identity_hash=source,
            output="healthy",
        )
    )
    writer = evaluate_writer_exclusivity_recovery_gate(
        scheduler,
        make_writer_inventory_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            writer_identities=(AUTHORITATIVE_UNIT,),
            authoritative_writer_identity=AUTHORITATIVE_UNIT,
            complete=True,
            source_identity_hash=source,
        ),
    )
    invariant = evaluate_protected_invariant_recovery_gate(
        writer,
        make_protected_invariant_observation(
            observed_at_epoch_seconds=100,
            evidence_age_seconds=age,
            paper_orders_count=204,
            position_sizing_max_id=239,
            position_sizing_count=239,
            advanced_risk_max_id=239,
            advanced_risk_count=239,
            protected_order_id=204,
            protected_order_status="filled",
            protected_order_ticker="KXRAINAUSM-26AUG-1",
            protected_order_quantity=1,
            protected_forecast_id=523912,
            protected_fill_count=1,
            phase3m_max_id=231,
            phase3n_max_id=231,
            complete=True,
            source_identity_hash=source,
        ),
    )
    return {
        "boot_identity": boot,
        "wsl_liveness": liveness,
        "keepalive_gap": keepalive,
        "user_systemd": systemd,
        "scheduler_health": scheduler,
        "writer_gate": writer,
        "invariant_gate": invariant,
    }
