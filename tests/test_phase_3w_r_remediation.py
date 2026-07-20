from pathlib import Path

from kalshi_predictor.config import Settings
from kalshi_predictor.data.db import get_session_factory, init_db
from kalshi_predictor.data.maintenance import migration_status
from kalshi_predictor.system_certification.connection_registry import (
    CONNECTION_REGISTRY,
    connection_registry_payload,
    render_endpoint,
    validate_connection_registry,
)
from kalshi_predictor.system_certification.contracts import (
    MODE_AUDIT_ONLY,
    MODE_LOCAL_INTEGRATION,
    STATUS_NOT_OBSERVED,
    STATUS_NOT_RUN,
    SYSTEM_INCOMPLETE,
)
from kalshi_predictor.system_certification.golden_trace import build_golden_trace
from kalshi_predictor.system_certification.migration_diagnostics import (
    ALEMBIC_AT_HEAD,
    ALEMBIC_ORPHANED_DATABASE_REVISION,
    ALEMBIC_UPGRADE_REQUIRED,
    alembic_graph_diagnostics,
)
from kalshi_predictor.system_certification.phase_registry import (
    IMPLEMENTED_UNVERIFIED,
    MANDATORY_PHASE_IDS,
    MAPPING_ERROR,
    PHASE_REGISTRY,
    phase_registry_payload,
    validate_phase_registry,
)
from kalshi_predictor.system_certification.reports import generate_system_certification_report
from kalshi_predictor.system_certification.service import SystemCertificationService


def test_phase_3w_r_authoritative_registry_covers_all_required_fields() -> None:
    assert len(PHASE_REGISTRY) == 29
    assert tuple(entry.phase_id for entry in PHASE_REGISTRY) == MANDATORY_PHASE_IDS
    assert validate_phase_registry() == []

    payload = phase_registry_payload(root=_repo_root())
    states = {
        row["phase_id"]: row["implementation_evidence"]["observed_state"]
        for row in payload["phases"]
    }
    for phase_id in ("3O", "3P", "3Q", "3R", "3S", "3T", "3U", "3V"):
        assert states[phase_id] in {IMPLEMENTED_UNVERIFIED}
        assert states[phase_id] != MAPPING_ERROR


def test_phase_3w_r_connection_registry_preserves_group_and_platform_edges() -> None:
    assert len(CONNECTION_REGISTRY) == 57
    assert validate_connection_registry() == []
    edge_by_id = {edge.connection_id: edge for edge in CONNECTION_REGISTRY}

    assert render_endpoint(edge_by_id["E054"].producer) == "3W"
    assert render_endpoint(edge_by_id["E054"].consumer) == "3V"
    assert render_endpoint(edge_by_id["E055"].producer) == "3C/3D/3T/3U"
    assert render_endpoint(edge_by_id["E055"].consumer) == "backend authorities"
    assert render_endpoint(edge_by_id["E056"].consumer) == "all durable phases"
    assert render_endpoint(edge_by_id["E057"].producer) == "all phases"
    assert render_endpoint(edge_by_id["E057"].consumer) == "observability"

    payload = connection_registry_payload()
    e055 = next(row for row in payload["connections"] if row["connection_id"] == "E055")
    assert len(e055["expanded_instances"]) == 4


def test_phase_3w_r_audit_mode_is_incomplete_not_false_fail(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        report = SystemCertificationService(session, settings=_settings(tmp_path)).build_report(
            mode=MODE_AUDIT_ONLY
        )

    assert report["overall_status"] == SYSTEM_INCOMPLETE
    assert report["technical_certification_status"] == SYSTEM_INCOMPLETE
    assert report["runtime_observation_status"] == STATUS_NOT_OBSERVED
    assert report["phase_3v_readiness_status"] == "NOT_READY"
    assert report["live_authorization_status"] == "NOT_AUTHORIZED"
    assert report["live_trading_authorized"] is False
    assert all(
        row["contract_test"]["status"] == STATUS_NOT_RUN
        for row in report["connection_results"]
    )


def test_phase_3w_r_local_integration_writes_required_artifacts(tmp_path) -> None:
    session_factory = _session_factory(tmp_path)
    output_dir = Path(tmp_path) / "cert"
    with session_factory() as session:
        report = generate_system_certification_report(
            session,
            output_dir=output_dir,
            settings=_settings(tmp_path),
            mode=MODE_LOCAL_INTEGRATION,
            run_contract_tests=True,
            run_golden_trace=True,
            persist=False,
        )

    assert report["live_trading_authorized"] is False
    assert report["golden_trace"]["status"] == "PASS"
    assert report["golden_trace"]["exchange_write_attempted"] is False
    assert (output_dir / "remediation_gap_report.md").exists()
    assert (output_dir / "repair_log.json").exists()
    assert (output_dir / "local" / "system_certification_report.json").exists()
    assert (output_dir / "local" / "system_certification_report.md").exists()
    assert (output_dir / "local" / "golden_trace.json").exists()
    assert (output_dir / "local" / "negative_scenario_traces.json").exists()
    assert (output_dir / "local" / "dynamic_no_bypass_evidence.json").exists()
    assert (output_dir / "local" / "test_evidence.json").exists()
    assert (output_dir / "local" / "runtime_access_statement.md").exists()


def test_phase_3w_r_local_integration_captures_dynamic_and_negative_evidence(
    tmp_path,
) -> None:
    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        report = generate_system_certification_report(
            session,
            output_dir=Path(tmp_path) / "cert",
            settings=_settings(tmp_path),
            mode=MODE_LOCAL_INTEGRATION,
            run_contract_tests=True,
            run_golden_trace=True,
            persist=False,
        )

    finding_ids = {finding["finding_id"] for finding in report["findings"]}
    assert "FIND-GOLDEN-TRACE-NOT-RUN" not in finding_ids
    assert "FIND-SCENARIO-EVIDENCE-INCOMPLETE" not in finding_ids
    assert "FIND-BYPASS-DYNAMIC-EVIDENCE" not in finding_ids
    assert finding_ids == {"FIND-RUNTIME-NOT-OBSERVED", "FIND-HUMAN-APPROVAL-MISSING"}
    assert report["golden_trace"]["status"] == "PASS"
    assert report["dynamic_no_bypass_evidence"]["status"] == "PASS"
    assert report["summary_counts"]["scenarios"]["pass"] == 10
    assert report["summary_counts"]["tests"]["not_run"] == 0
    assert report["summary_counts"]["tests"]["failed"] == 0
    assert report["not_run_items"] == []
    assert all(
        trace["exchange_write_attempted"] is False
        and trace["demo_order_attempted"] is False
        and trace["paper_order_submitted"] is False
        for trace in report["negative_scenario_traces"].values()
    )


def test_phase_3w_r_alembic_ancestry_classification(tmp_path) -> None:
    diagnostics = alembic_graph_diagnostics(["20260716_0012"], root=_repo_root())
    assert diagnostics["status"] == ALEMBIC_AT_HEAD

    behind = alembic_graph_diagnostics(["20260624_0011"], root=_repo_root())
    assert behind["status"] == ALEMBIC_UPGRADE_REQUIRED

    orphan = alembic_graph_diagnostics(["missing_revision"], root=_repo_root())
    assert orphan["status"] == ALEMBIC_ORPHANED_DATABASE_REVISION

    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        status = migration_status(session=session, settings=_settings(tmp_path))
    assert status["graph_status"] in {ALEMBIC_UPGRADE_REQUIRED, ALEMBIC_AT_HEAD}
    assert status["head_revision"] == "20260716_0012"


def test_phase_3w_r_golden_trace_is_deterministic_and_no_exchange_write() -> None:
    first = build_golden_trace(executed=True)
    second = build_golden_trace(executed=True)

    assert first == second
    assert first["live_trading_authorized"] is False
    assert first["exchange_write_attempted"] is False
    assert first["demo_order_attempted"] is False
    assert first["synthetic_entered_tradable_path"] is False


def _settings(tmp_path) -> Settings:
    return Settings(
        phase_3w_system_certification_enabled=True,
        phase_3w_mode="AUDIT_ONLY",
        phase_3w_output_dir=str(Path(tmp_path) / "cert"),
    )


def _session_factory(tmp_path):
    engine = init_db(f"sqlite:///{Path(tmp_path) / 'phase3wr.db'}")
    return get_session_factory(engine)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
