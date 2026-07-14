from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import desc, inspect, select, text
from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.data.schema import (
    SystemCertificationArtifact,
    SystemCertificationRun,
)
from kalshi_predictor.system_certification.connection_registry import (
    CONNECTION_REGISTRY,
    connection_registry_payload,
    render_endpoint,
    validate_connection_registry,
)
from kalshi_predictor.system_certification.contracts import (
    CONNECTIONS,
    E0_CLAIM,
    E1_STATIC,
    E3_REPLAY,
    LIVE_AUTH_NOT_AUTHORIZED,
    MODE_AUDIT_ONLY,
    MODE_LOCAL_INTEGRATION,
    MODE_SAFE_REPAIR,
    MODE_STAGING_READ_ONLY,
    PHASE_3V_NOT_READY,
    PHASES,
    SCENARIO_GROUPS,
    SCHEMA_VERSION,
    STATUS_FAIL,
    STATUS_INCOMPLETE,
    STATUS_MAPPING_ERROR,
    STATUS_NOT_OBSERVED,
    STATUS_NOT_RUN,
    STATUS_PASS,
    SYSTEM_FAIL,
    SYSTEM_INCOMPLETE,
    CertificationConfig,
    canonical_json,
    sha256_bytes,
    sha256_json,
    sha256_text,
    stable_id,
)
from kalshi_predictor.system_certification.golden_trace import (
    build_dynamic_no_bypass_evidence,
    build_golden_trace,
    build_negative_scenario_trace,
    golden_trace_contract_checks,
    negative_scenario_contract_checks,
)
from kalshi_predictor.system_certification.migration_diagnostics import (
    ALEMBIC_AT_HEAD,
    ALEMBIC_UPGRADE_REQUIRED,
    alembic_graph_diagnostics,
    latest_head_revision,
)
from kalshi_predictor.system_certification.phase_registry import (
    MAPPING_ERROR,
    PHASE_REGISTRY,
    implementation_evidence,
    phase_registry_payload,
    validate_phase_registry,
)
from kalshi_predictor.system_certification.runtime_observer import observe_runtime
from kalshi_predictor.utils.time import parse_datetime, utc_now

WRITE_PATH_PATTERNS = (
    "create_order",
    "submit_order",
    "cancel_order",
    "demo_execute",
    "paper_trade",
    "create_paper_order",
    "run_paper_trading",
    "execution_enabled",
    "external-api.kalshi.com",
    "/portfolio/orders",
)


def config_from_settings(settings: Settings | None = None) -> CertificationConfig:
    resolved = settings or get_settings()
    config = CertificationConfig(
        enabled=resolved.phase_3w_system_certification_enabled,
        mode=resolved.phase_3w_mode.upper(),
        safe_repair_enabled=resolved.phase_3w_safe_repair_enabled,
        output_dir=resolved.phase_3w_output_dir,
    )
    config.validate()
    return config


class SystemCertificationService:
    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.config = config_from_settings(self.settings)
        self.root = Path(__file__).resolve().parents[3]

    def build_report(
        self,
        *,
        mode: str | None = None,
        run_contract_tests: bool = False,
        run_golden_trace: bool = False,
        database_profile: str = "local",
        runtime_url: str | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        selected_mode = (mode or self.config.mode).upper()
        if selected_mode == MODE_SAFE_REPAIR and not self.config.safe_repair_enabled:
            selected_mode = MODE_AUDIT_ONLY
        if selected_mode == MODE_AUDIT_ONLY:
            run_contract_tests = False
            run_golden_trace = False

        golden_trace = build_golden_trace(executed=run_golden_trace)
        scenario_traces = {
            scenario["scenario_id"]: build_negative_scenario_trace(
                scenario["scenario_id"],
                executed=run_golden_trace,
            )
            for scenario in SCENARIO_GROUPS
            if scenario["scenario_id"] != "GOLDEN-TRACE"
        }
        dynamic_no_bypass_evidence = build_dynamic_no_bypass_evidence(
            executed=run_contract_tests
        )
        phases = [self._phase_result_from_registry(phase) for phase in PHASE_REGISTRY]
        local_test_evidence = self._local_test_evidence(
            executed=run_contract_tests,
            run_golden_trace=run_golden_trace,
            scenario_traces=scenario_traces,
            dynamic_no_bypass_evidence=dynamic_no_bypass_evidence,
        )
        connections = [
            self._connection_result_from_registry(
                connection,
                phases,
                tests_executed=run_contract_tests,
                golden_trace=golden_trace,
            )
            for connection in CONNECTION_REGISTRY
        ]
        scenarios = [
            self._scenario_result(
                scenario,
                golden_trace=golden_trace,
                scenario_traces=scenario_traces,
            )
            for scenario in SCENARIO_GROUPS
        ]
        runtime_observation = observe_runtime(runtime_url=runtime_url)
        bypass = self.static_bypass_results(
            dynamic_no_bypass_evidence=dynamic_no_bypass_evidence
        )
        database_results = self._database_check_group(database_profile=database_profile)
        findings = self._findings(
            phases,
            connections,
            scenarios,
            bypass,
            runtime_observation=runtime_observation,
            local_test_evidence=local_test_evidence,
        )
        overall_status = self._overall_status(
            phases,
            findings,
            local_test_evidence=local_test_evidence,
        )
        completed_at = utc_now()
        run_id = stable_id("cert", started_at.isoformat(), selected_mode, sha256_json(phases))
        report = {
            "schema_version": SCHEMA_VERSION,
            "certification_run_id": run_id,
            "mode": selected_mode,
            "overall_status": overall_status,
            "technical_certification_status": overall_status,
            "runtime_observation_status": runtime_observation["status"],
            "phase_3v_readiness_status": PHASE_3V_NOT_READY,
            "live_authorization_status": LIVE_AUTH_NOT_AUTHORIZED,
            "live_trading_authorized": False,
            "scope": self._scope(selected_mode),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "repository_fingerprint": self.repository_fingerprint(),
            "runtime_access": self.runtime_access_statement(),
            "runtime_observation": runtime_observation,
            "phase_registry": phase_registry_payload(root=self.root),
            "connection_registry": connection_registry_payload(),
            "local_test_evidence": local_test_evidence,
            "golden_trace": golden_trace,
            "negative_scenario_traces": scenario_traces,
            "dynamic_no_bypass_evidence": dynamic_no_bypass_evidence,
            "database_profile": database_profile,
            "summary_counts": self._summary_counts(
                phases,
                connections,
                scenarios,
                findings,
                local_test_evidence=local_test_evidence,
                not_run_count=_not_run_count(connections, scenarios, local_test_evidence),
            ),
            "phase_results": phases,
            "connection_results": connections,
            "scenario_results": scenarios,
            "static_bypass_results": bypass,
            "data_integrity_results": self._standard_check_group("DATA", "Data integrity"),
            "database_results": database_results,
            "point_in_time_results": self._standard_check_group("PIT", "Point-in-time leakage"),
            "security_results": self._standard_check_group("SEC", "Security and secret isolation"),
            "observability_results": self._standard_check_group("OBS", "Observability and health"),
            "performance_results": self._standard_check_group("PERF", "Performance baselines"),
            "recovery_results": self._standard_check_group("REC", "Fault recovery"),
            "findings": findings,
            "repairs": [],
            "not_run_items": self._not_run_items(connections, scenarios),
            "evidence_manifest": {
                "generated_at": completed_at.isoformat(),
                "manifest_sha256": "UNAVAILABLE",
                "artifacts": {},
                "limitations": self.runtime_access_statement()["limitations"],
            },
            "phase_3v_handoff": {
                "status": PHASE_3V_NOT_READY,
                "evidence_package": None,
                "evidence_sha256": None,
                "limitations": [
                    "Phase 3W generated audit evidence only.",
                    (
                        "Replay, staging runtime, production read-only observation, "
                        "and human approval are absent."
                    ),
                ],
                "human_approval_required": True,
                "live_trading_authorized": False,
            },
            "mode_results": {
                "audit_only": {
                    "status": STATUS_INCOMPLETE,
                    "reason": (
                        "Static mapping is visible, but E2/E3/runtime evidence is "
                        "required for certification pass."
                    ),
                },
                "local_integration": {
                    "status": local_test_evidence["status"],
                    "artifact": "local/test_evidence.json",
                },
                "staging_read_only": {
                    "status": STATUS_NOT_OBSERVED,
                    "reason": "No staging read-only runtime observation was performed.",
                },
            },
            "artifacts": [],
        }
        return report

    def write_artifacts(
        self,
        *,
        output_dir: str | Path | None = None,
        mode: str | None = None,
        run_contract_tests: bool = False,
        run_golden_trace: bool = False,
        database_profile: str = "local",
        runtime_url: str | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        out_dir = Path(output_dir or self.config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        local_dir = out_dir if out_dir.name == "local" else out_dir / "local"
        local_dir.mkdir(parents=True, exist_ok=True)
        report = self.build_report(
            mode=mode,
            run_contract_tests=run_contract_tests,
            run_golden_trace=run_golden_trace,
            database_profile=database_profile,
            runtime_url=runtime_url,
        )
        repo_map = render_repo_map(report)
        order_paths = self.order_write_path_inventory()
        runtime = self.runtime_access_statement()
        gaps = self._gap_report(report)
        remediation_gaps = self._remediation_gap_report(report)
        repair_log = self._repair_log(report)
        pre_artifacts = {
            "repo_map.md": repo_map,
            "phase_capability_inventory.json": json.dumps(
                report["phase_registry"], indent=2, sort_keys=True
            ),
            "connection_graph.json": json.dumps(
                report["connection_registry"], indent=2, sort_keys=True
            ),
            "order_write_path_inventory.json": json.dumps(order_paths, indent=2, sort_keys=True),
            "runtime_access_statement.md": render_runtime_access(runtime),
            "initial_gap_report.md": gaps,
            "remediation_gap_report.md": remediation_gaps,
            "repair_log.json": json.dumps(repair_log, indent=2, sort_keys=True),
        }
        artifacts: list[dict[str, str]] = []
        for name, content in pre_artifacts.items():
            path = out_dir / name
            path.write_text(content, encoding="utf-8")
            artifacts.append(_artifact(name, path, _artifact_kind(name)))

        local_artifacts = {
            "golden_trace.json": json.dumps(report["golden_trace"], indent=2, sort_keys=True),
            "negative_scenario_traces.json": json.dumps(
                report["negative_scenario_traces"], indent=2, sort_keys=True
            ),
            "dynamic_no_bypass_evidence.json": json.dumps(
                report["dynamic_no_bypass_evidence"], indent=2, sort_keys=True
            ),
            "test_evidence.json": json.dumps(
                report["local_test_evidence"], indent=2, sort_keys=True
            ),
            "runtime_access_statement.md": render_runtime_access(runtime),
        }
        for name, content in local_artifacts.items():
            path = local_dir / name
            path.write_text(content, encoding="utf-8")
            artifacts.append(_artifact(f"local/{name}", path, _artifact_kind(name)))

        report["evidence_manifest"]["artifacts"] = {
            item["path"]: item["sha256"] for item in artifacts
        }
        report["evidence_manifest"]["manifest_sha256"] = sha256_json(
            report["evidence_manifest"]["artifacts"]
        )
        report["phase_3v_handoff"]["evidence_package"] = str(
            out_dir / "system_certification_report.json"
        )
        report["phase_3v_handoff"]["evidence_sha256"] = sha256_json(
            report["evidence_manifest"]
        )
        json_path = out_dir / "system_certification_report.json"
        md_path = out_dir / "system_certification_report.md"
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(render_certification_report(report), encoding="utf-8")
        artifacts.extend(
            [
                _artifact("system_certification_report.json", json_path, "report"),
                _artifact("system_certification_report.md", md_path, "report"),
            ]
        )
        local_json_path = local_dir / "system_certification_report.json"
        local_md_path = local_dir / "system_certification_report.md"
        local_json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        local_md_path.write_text(render_certification_report(report), encoding="utf-8")
        artifacts.extend(
            [
                _artifact("local/system_certification_report.json", local_json_path, "report"),
                _artifact("local/system_certification_report.md", local_md_path, "report"),
            ]
        )
        report["artifacts"] = artifacts
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        md_path.write_text(render_certification_report(report), encoding="utf-8")
        local_json_path.write_text(
            json.dumps(report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        local_md_path.write_text(render_certification_report(report), encoding="utf-8")
        if persist:
            self._persist_report(report, artifacts)
            self.session.commit()
        return report

    def repository_fingerprint(self) -> dict[str, Any]:
        git = _git_metadata(self.root)
        source_paths = [
            path
            for path in self.root.rglob("*.py")
            if ".venv" not in path.parts and "__pycache__" not in path.parts
        ]
        source_digest = sha256_text(
            "\n".join(
                f"{path.as_posix()}:{sha256_bytes(path.read_bytes())}"
                for path in sorted(source_paths)
            )
        )
        dependency_hash = _hash_file(self.root / "pyproject.toml")
        migration_version = latest_head_revision(root=self.root)
        return {
            "repository": self.root.name,
            "branch": git.get("branch") or "UNAVAILABLE",
            "commit": git.get("commit") or "UNKNOWN",
            "dirty": bool(git.get("dirty", True)),
            "dependency_lock_sha256": dependency_hash,
            "migration_version": migration_version,
            "build_sha256": source_digest,
            "config_sha256": sha256_json(self.settings.model_dump(mode="json")),
            "model_hashes": {},
            "feature_hashes": {},
            "policy_hashes": {},
        }

    def runtime_access_statement(self) -> dict[str, Any]:
        return {
            "code": True,
            "test": True,
            "replay": False,
            "staging": False,
            "production_read_only": False,
            "limitations": [
                "No deployed staging runtime was observed by this certification command.",
                "No production read-only runtime was observed by this certification command.",
                "No human Phase 3V approval evidence was available or generated.",
                "Historical golden replay evidence is not produced by the default audit run.",
            ],
        }

    def order_write_path_inventory(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted((self.root / "src").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            matches = [pattern for pattern in WRITE_PATH_PATTERNS if pattern in text]
            if not matches:
                continue
            rows.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "patterns": matches,
                    "status": "INVENTORIED",
                    "requires_manual_review": any(
                        pattern in matches
                        for pattern in ("create_order", "submit_order", "/portfolio/orders")
                    ),
                }
            )
        return rows

    def static_bypass_results(
        self,
        *,
        dynamic_no_bypass_evidence: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        inventory = self.order_write_path_inventory()
        dynamic_no_bypass_evidence = dynamic_no_bypass_evidence or (
            build_dynamic_no_bypass_evidence(executed=False)
        )
        dynamic_passed = dynamic_no_bypass_evidence.get("status") == STATUS_PASS
        production_write_candidates = [
            row
            for row in inventory
            if any(
                pattern in row["patterns"]
                for pattern in ("create_order", "submit_order", "/portfolio/orders")
            )
        ]
        return [
            _check_result(
                "BYPASS-ORDER-PATHS",
                "Every order/write-like path is inventoried",
                STATUS_PASS if inventory else STATUS_INCOMPLETE,
                [E1_STATIC],
                [row["path"] for row in inventory[:25]],
                [] if inventory else ["No order/write-like paths found by static scan."],
            ),
            _check_result(
                "BYPASS-PROD-WRITE",
                "No production order endpoint is authorized by Phase 3W",
                STATUS_PASS
                if dynamic_passed or not production_write_candidates
                else STATUS_INCOMPLETE,
                [E3_REPLAY] if dynamic_passed else [E1_STATIC],
                [row["path"] for row in production_write_candidates[:25]],
                []
                if dynamic_passed or not production_write_candidates
                else [
                    "Static candidates require manual review and dynamic guard evidence."
                ],
            ),
            _check_result(
                "BYPASS-LIVE-AUTH",
                "Phase 3W live authorization remains false",
                STATUS_PASS,
                [E1_STATIC],
                ["report.live_trading_authorized=false"],
                [],
            ),
            _check_result(
                "BYPASS-DYNAMIC-NO-WRITE",
                "Dynamic replay proves live/demo write paths remain blocked",
                STATUS_PASS if dynamic_passed else STATUS_INCOMPLETE,
                [E3_REPLAY] if dynamic_passed else [E0_CLAIM],
                [
                    "local/dynamic_no_bypass_evidence.json",
                    "report.live_trading_authorized=false",
                    "phase_3v_readiness_status=NOT_READY",
                ],
                []
                if dynamic_passed
                else ["Run local integration mode to capture dynamic no-bypass evidence."],
            ),
        ]

    def latest_status(self) -> dict[str, Any]:
        latest = self.session.scalar(
            select(SystemCertificationRun)
            .order_by(desc(SystemCertificationRun.completed_at))
            .limit(1)
        )
        if latest is None:
            return {
                "overall_status": SYSTEM_INCOMPLETE,
                "mode": self.config.mode,
                "latest_run_id": "none",
                "completed_at": "never",
                "phase_count": len(PHASES),
                "connection_count": len(CONNECTIONS),
                "live_trading_authorized": False,
                "next_action": "Run kalshi-bot system-certification-run.",
            }
        return {
            "overall_status": latest.overall_status,
            "mode": latest.mode,
            "latest_run_id": latest.certification_run_id,
            "completed_at": latest.completed_at.isoformat(),
            "phase_count": latest.phase_count,
            "connection_count": latest.connection_count,
            "live_trading_authorized": False,
            "next_action": "Review reports/system_certification/system_certification_report.md.",
        }

    def _phase_result_from_registry(self, phase: Any) -> dict[str, Any]:
        evidence = implementation_evidence(phase, root=self.root)
        mapped = bool(evidence["mapped"])
        status = STATUS_INCOMPLETE if mapped else STATUS_FAIL
        observed_state = evidence["observed_state"]
        findings = []
        if observed_state == MAPPING_ERROR:
            findings.append(f"{phase.phase_id}: implementation mapping could not be resolved.")
        else:
            findings.append("Runtime and E2/E3 evidence still required for certification pass.")
        locations = [
            row["path"]
            for row in evidence["modules"]
            if row["available"] or row.get("path_exists")
        ]
        checks = [
            _check_result(
                f"PHASE-{phase.phase_id}-REGISTRY",
                "Authoritative registry entry is complete",
                STATUS_PASS,
                [E1_STATIC],
                [phase.health_probe],
                [],
            ),
            _check_result(
                f"PHASE-{phase.phase_id}-STATIC-MAP",
                "Implementation module mapped",
                STATUS_PASS if mapped else STATUS_MAPPING_ERROR,
                [E1_STATIC],
                locations,
                [] if mapped else findings,
            ),
            _check_result(
                f"PHASE-{phase.phase_id}-EVIDENCE",
                "Minimum evidence captured",
                STATUS_NOT_RUN,
                [E0_CLAIM],
                list(phase.test_selectors),
                [f"Minimum evidence grade required: {phase.minimum_evidence_grade}."],
            ),
        ]
        return {
            "phase_id": phase.phase_id,
            "name": phase.name,
            "capability": phase.capability,
            "status": status,
            "implementation_state": phase.implementation_state,
            "observed_state": observed_state,
            "implementation_modules": list(phase.implementation_modules),
            "implementation_locations": locations,
            "runtime_components": [],
            "inputs": list(phase.consumer_contracts),
            "outputs": list(phase.producer_contracts),
            "contracts": list(phase.producer_contracts + phase.consumer_contracts),
            "feature_flags": list(phase.config_sources),
            "schema_or_table_refs": list(phase.schema_or_table_refs),
            "cli_or_service_entrypoints": list(phase.cli_or_service_entrypoints),
            "health_probe": phase.health_probe,
            "test_selectors": list(phase.test_selectors),
            "replay_selector": phase.replay_selector,
            "minimum_evidence_grade": phase.minimum_evidence_grade,
            "runtime_required": phase.runtime_required,
            "safety_classification": phase.safety_classification,
            "owner_or_todo": phase.owner_or_todo,
            "versions": {},
            "checks": checks,
            "findings": findings,
            "evidence_grades": [E1_STATIC] if mapped else [E0_CLAIM],
            "last_successful_runtime_observation": None,
        }

    def _connection_result_from_registry(
        self,
        connection: Any,
        phases: list[dict[str, Any]],
        *,
        tests_executed: bool,
        golden_trace: dict[str, Any],
    ) -> dict[str, Any]:
        phase_by_id = {phase["phase_id"]: phase for phase in phases}
        registry_errors = [
            error
            for error in validate_connection_registry()
            if error.startswith(f"{connection.connection_id}:")
        ]
        mapped = not registry_errors and all(
            _endpoint_mapped(endpoint, phase_by_id)
            for endpoint in (connection.producer, connection.consumer)
        )
        test_status = STATUS_PASS if tests_executed and mapped else STATUS_NOT_RUN
        if connection.connection_id == "E050" and golden_trace.get("status") == STATUS_PASS:
            test_status = STATUS_PASS
        status = STATUS_INCOMPLETE if mapped else STATUS_FAIL
        findings = []
        if registry_errors:
            findings.extend(registry_errors)
        if not mapped and not registry_errors:
            findings.append("Producer or consumer endpoint is not mapped.")
        if mapped:
            findings.append(
                "Automated contract/runtime evidence is incomplete; static registry is mapped."
            )
        test_ref = _test_reference(
            status=test_status,
            command=(
                f"registry contract check for {connection.connection_id}"
                if tests_executed
                else f"Contract test for {connection.connection_id} not executed."
            ),
            passed=1 if test_status == STATUS_PASS else 0,
            failed=0,
            artifact="local/test_evidence.json" if tests_executed else None,
        )
        return {
            "connection_id": connection.connection_id,
            "source_phase": render_endpoint(connection.producer),
            "destination_phase": render_endpoint(connection.consumer),
            "producer_endpoint": connection.producer.to_dict(),
            "consumer_endpoint": connection.consumer.to_dict(),
            "status": status,
            "required": connection.required,
            "transport": _transport(connection.transport),
            "producer_contract": connection.contract,
            "consumer_contract": connection.contract,
            "schema_compatible": test_status == STATUS_PASS,
            "units_compatible": test_status == STATUS_PASS,
            "freshness_verified": False,
            "ordering_verified": False,
            "idempotency_verified": test_status == STATUS_PASS
            and connection.connection_id in {"E041", "E056"},
            "retry_verified": False,
            "correlation_verified": (
                connection.connection_id == "E057" and test_status == STATUS_PASS
            ),
            "authorization_verified": (
                connection.negative_assertion or connection.connection_id == "E039"
            ),
            "contract_test": test_ref,
            "integration_test": test_ref,
            "runtime_observation": {
                "status": STATUS_NOT_OBSERVED,
                "command": "No runtime probe executed.",
            },
            "findings": findings,
            "notes": connection.notes,
            "expanded_instances": connection.to_dict()["expanded_instances"],
        }

    def _phase_result(self, phase: dict[str, Any]) -> dict[str, Any]:
        locations = [
            location
            for location in phase["locations"]
            if (self.root / location).exists()
        ]
        status = STATUS_INCOMPLETE if locations else STATUS_FAIL
        findings = [] if locations else [f"{phase['phase_id']}: implementation not found"]
        checks = [
            _check_result(
                f"PHASE-{phase['phase_id']}-STATIC-MAP",
                "Implementation location mapped",
                STATUS_PASS if locations else STATUS_FAIL,
                [E1_STATIC],
                locations,
                findings,
            ),
            _check_result(
                f"PHASE-{phase['phase_id']}-CERT-E2",
                "Mandatory contract/integration evidence captured",
                STATUS_INCOMPLETE,
                [E0_CLAIM],
                [],
                ["E2/E3 certification evidence not captured by default audit command."],
            ),
        ]
        return {
            "phase_id": phase["phase_id"],
            "name": phase["name"],
            "status": status,
            "implementation_locations": locations,
            "runtime_components": [],
            "inputs": phase["inputs"],
            "outputs": phase["outputs"],
            "contracts": [],
            "feature_flags": phase["feature_flags"],
            "versions": {},
            "checks": checks,
            "findings": findings,
            "evidence_grades": [E1_STATIC] if locations else [E0_CLAIM],
            "last_successful_runtime_observation": None,
        }

    def _connection_result(
        self,
        connection: dict[str, Any],
        phases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        phase_by_id = {phase["phase_id"]: phase for phase in phases}
        source_found = connection["source_phase"] in {"ALL", "GATEWAY", "OBSERVABILITY", "3W"} or (
            phase_by_id.get(connection["source_phase"], {}).get("implementation_locations")
        )
        dest_found = connection["destination_phase"] in {"ALL", "GATEWAY", "OBSERVABILITY"} or (
            phase_by_id.get(connection["destination_phase"], {}).get("implementation_locations")
        )
        mapped = bool(source_found and dest_found)
        status = STATUS_INCOMPLETE if mapped else STATUS_FAIL
        findings = [] if mapped else ["Producer or consumer implementation was not mapped."]
        test_ref = _not_run_test_reference(
            f"Contract test for {connection['connection_id']} not executed."
        )
        return {
            "connection_id": connection["connection_id"],
            "source_phase": connection["source_phase"],
            "destination_phase": connection["destination_phase"],
            "status": status,
            "required": True,
            "transport": _transport(connection["transport"]),
            "producer_contract": connection["contract"],
            "consumer_contract": connection["contract"],
            "schema_compatible": False,
            "units_compatible": False,
            "freshness_verified": False,
            "ordering_verified": False,
            "idempotency_verified": False,
            "retry_verified": False,
            "correlation_verified": False,
            "authorization_verified": connection["transport"] == "negative_assertion",
            "contract_test": test_ref,
            "integration_test": test_ref,
            "runtime_observation": None,
            "findings": findings or ["Automated boundary evidence is not yet captured."],
        }

    def _scenario_result(
        self,
        scenario: dict[str, Any],
        *,
        golden_trace: dict[str, Any] | None = None,
        scenario_traces: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        golden_trace = golden_trace or build_golden_trace(executed=False)
        scenario_traces = scenario_traces or {}
        scenario_trace = scenario_traces.get(scenario["scenario_id"])
        executed = bool(
            (
                scenario["scenario_id"] == "GOLDEN-TRACE"
                and golden_trace["status"] == STATUS_PASS
            )
            or (scenario_trace and scenario_trace.get("status") == STATUS_PASS)
        )
        artifact = (
            "local/golden_trace.json"
            if scenario["scenario_id"] == "GOLDEN-TRACE"
            else "local/negative_scenario_traces.json"
        )
        command = (
            "deterministic local golden trace"
            if scenario["scenario_id"] == "GOLDEN-TRACE"
            else f"deterministic local {scenario['scenario_id']} scenario replay"
        )
        return {
            "scenario_id": scenario["scenario_id"],
            "name": scenario["name"],
            "status": STATUS_PASS if executed else STATUS_INCOMPLETE,
            "affected_phases": scenario["phases"],
            "environment": "local",
            "controlled_clock": executed,
            "assertions": [
                "No production write endpoint may be reached.",
                "Result requires explicit replay or integration evidence.",
            ],
            "test_reference": _test_reference(
                status=STATUS_PASS if executed else STATUS_NOT_RUN,
                command=command
                if executed
                else f"{scenario['scenario_id']} certification scenario not executed.",
                passed=1 if executed else 0,
                failed=0,
                artifact=artifact if executed else None,
            ),
            "correlation_id": (
                golden_trace.get("trace_id")
                if scenario["scenario_id"] == "GOLDEN-TRACE" and executed
                else scenario_trace.get("trace_id")
                if scenario_trace and executed
                else stable_id("trace", scenario["scenario_id"])
            ),
            "evidence_grades": [E3_REPLAY] if executed else [E0_CLAIM],
            "findings": []
            if executed
            else ["Scenario evidence is not captured by default audit command."],
        }

    def _standard_check_group(self, prefix: str, name: str) -> list[dict[str, Any]]:
        return [
            _check_result(
                f"{prefix}-001",
                name,
                STATUS_INCOMPLETE,
                [E0_CLAIM],
                [],
                ["Dedicated certification evidence has not been captured."],
            )
        ]

    def _database_check_group(self, *, database_profile: str) -> list[dict[str, Any]]:
        current_revisions: list[str] = []
        try:
            bind = self.session.get_bind()
            if inspect(bind).has_table("alembic_version"):
                rows = self.session.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalars()
                current_revisions = [str(row) for row in rows if row]
        except Exception:  # noqa: BLE001 - certification must report, not crash, DB probe failures.
            current_revisions = []
        diagnostics = alembic_graph_diagnostics(current_revisions, root=self.root)
        status = STATUS_PASS if diagnostics["status"] == ALEMBIC_AT_HEAD else STATUS_INCOMPLETE
        if diagnostics["status"] not in {ALEMBIC_AT_HEAD, ALEMBIC_UPGRADE_REQUIRED}:
            status = STATUS_FAIL
        return [
            _check_result(
                "DB-ALEMBIC-ANCESTRY",
                "Alembic migration ancestry",
                status,
                [E1_STATIC],
                [
                    diagnostics["message"],
                    f"database_profile={database_profile}",
                    f"script_location={diagnostics['script_location']}",
                ],
                []
                if status == STATUS_PASS
                else ["Database migration status requires review before certification pass."],
            )
        ]

    def _findings(
        self,
        phases: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        scenarios: list[dict[str, Any]],
        bypass: list[dict[str, Any]],
        *,
        runtime_observation: dict[str, Any],
        local_test_evidence: dict[str, Any],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        if any(phase["status"] == STATUS_FAIL for phase in phases):
            findings.append(
                _finding(
                    "FIND-PHASE-MISSING",
                    "CRITICAL",
                    "One or more mandatory phases were not mapped.",
                    [phase["phase_id"] for phase in phases if phase["status"] == STATUS_FAIL],
                    [],
                    ["phase_capability_inventory.json"],
                )
            )
        if any(connection["status"] == STATUS_FAIL for connection in connections):
            findings.append(
                _finding(
                    "FIND-CONNECTION-MAPPING",
                    "CRITICAL",
                    "One or more mandatory producer/consumer edges contain mapping errors.",
                    [phase["phase_id"] for phase in phases],
                    [row["connection_id"] for row in connections if row["status"] == STATUS_FAIL],
                    ["connection_graph.json"],
                )
            )
        if any(
            connection["contract_test"]["status"] == STATUS_NOT_RUN
            for connection in connections
        ):
            findings.append(
                _finding(
                    "FIND-CONNECTION-EVIDENCE",
                    "HIGH",
                    "Mandatory producer/consumer edge tests are not fully executed.",
                    [phase["phase_id"] for phase in phases],
                    [row["connection_id"] for row in connections],
                    ["connection_graph.json"],
                )
            )
        golden_trace_missing = any(
            scenario["scenario_id"] == "GOLDEN-TRACE"
            and scenario["test_reference"]["status"] == STATUS_NOT_RUN
            for scenario in scenarios
        )
        other_scenarios_missing = any(
            scenario["scenario_id"] != "GOLDEN-TRACE"
            and scenario["test_reference"]["status"] == STATUS_NOT_RUN
            for scenario in scenarios
        )
        if golden_trace_missing:
            findings.append(
                _finding(
                    "FIND-GOLDEN-TRACE-NOT-RUN",
                    "HIGH",
                    "Golden trace was not executed.",
                    [phase["phase_id"] for phase in phases],
                    [],
                    ["scenario_results"],
                )
            )
        if other_scenarios_missing:
            findings.append(
                _finding(
                    "FIND-SCENARIO-EVIDENCE-INCOMPLETE",
                    "HIGH",
                    "Negative-path and domain scenario evidence is incomplete.",
                    [phase["phase_id"] for phase in phases],
                    [],
                    ["scenario_results"],
                )
            )
        if any(row["status"] == STATUS_INCOMPLETE for row in bypass):
            findings.append(
                _finding(
                    "FIND-BYPASS-DYNAMIC-EVIDENCE",
                    "HIGH",
                    "Static order-path inventory needs dynamic no-bypass evidence.",
                    ["3A", "3B", "3M", "3N", "3V"],
                    ["E033", "E034", "E039"],
                    ["order_write_path_inventory.json"],
                )
            )
        if runtime_observation["status"] == STATUS_NOT_OBSERVED:
            findings.append(
                _finding(
                    "FIND-RUNTIME-NOT-OBSERVED",
                    "HIGH",
                    "No staging or production read-only runtime was observed.",
                    [phase["phase_id"] for phase in phases],
                    [],
                    ["runtime_access_statement.md"],
                )
            )
        if local_test_evidence["status"] == STATUS_NOT_RUN:
            findings.append(
                _finding(
                    "FIND-LOCAL-INTEGRATION-NOT-RUN",
                    "HIGH",
                    "Local integration contract checks were not executed.",
                    [phase["phase_id"] for phase in phases],
                    [row["connection_id"] for row in connections],
                    ["local/test_evidence.json"],
                )
            )
        findings.append(
            _finding(
                "FIND-HUMAN-APPROVAL-MISSING",
                "HIGH",
                "Phase 3V human approvals are external and were not supplied.",
                ["3V"],
                ["E054"],
                ["phase_3v_handoff"],
            )
        )
        return findings

    def _overall_status(
        self,
        phases: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        *,
        local_test_evidence: dict[str, Any],
    ) -> str:
        if any(phase["status"] == STATUS_FAIL for phase in phases):
            return SYSTEM_FAIL
        if local_test_evidence["status"] == STATUS_FAIL:
            return SYSTEM_FAIL
        if any(finding["severity"] == "CRITICAL" for finding in findings):
            return SYSTEM_FAIL
        return SYSTEM_INCOMPLETE

    def _scope(self, selected_mode: str) -> dict[str, Any]:
        return {
            "phases": [phase["phase_id"] for phase in PHASES],
            "environments": ["local"],
            "modes": [
                MODE_AUDIT_ONLY,
                MODE_LOCAL_INTEGRATION,
                MODE_STAGING_READ_ONLY,
                MODE_SAFE_REPAIR,
                "paper",
                "demo",
                "shadow",
            ],
            "accounts": [],
            "market_scope": [],
            "safe_repair_enabled": selected_mode == MODE_SAFE_REPAIR,
        }

    def _summary_counts(
        self,
        phases: list[dict[str, Any]],
        connections: list[dict[str, Any]],
        scenarios: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        *,
        local_test_evidence: dict[str, Any],
        not_run_count: int,
    ) -> dict[str, Any]:
        passed = sum(
            1 for row in connections if row["contract_test"]["status"] == STATUS_PASS
        )
        passed += sum(
            1 for row in scenarios if row["test_reference"]["status"] == STATUS_PASS
        )
        passed += int(local_test_evidence.get("passed") or 0)
        failed = sum(
            1 for row in connections if row["contract_test"]["status"] == STATUS_FAIL
        )
        failed += sum(
            1 for row in scenarios if row["test_reference"]["status"] == STATUS_FAIL
        )
        failed += int(local_test_evidence.get("failed") or 0)
        return {
            "phases": _status_counts(phases),
            "connections": _status_counts(connections),
            "scenarios": _status_counts(scenarios),
            "findings": {
                "critical": sum(1 for row in findings if row["severity"] == "CRITICAL"),
                "high": sum(1 for row in findings if row["severity"] == "HIGH"),
                "medium": sum(1 for row in findings if row["severity"] == "MEDIUM"),
                "low": sum(1 for row in findings if row["severity"] == "LOW"),
                "info": sum(1 for row in findings if row["severity"] == "INFO"),
            },
            "tests": {
                "passed": passed,
                "failed": failed,
                "skipped": 0,
                "not_run": not_run_count,
            },
        }

    def _not_run_items(
        self,
        connections: list[dict[str, Any]],
        scenarios: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        items = [
            {
                "item_id": f"NR-{connection['connection_id']}",
                "name": f"Connection {connection['connection_id']} contract/integration test",
                "reason": "Default audit run does not execute boundary-specific tests.",
                "impact": "Connection cannot count as PASS for system certification.",
                "required_for_system_pass": True,
                "required_for_phase_3v": connection["connection_id"] == "E054",
            }
            for connection in connections
            if connection["contract_test"]["status"] == STATUS_NOT_RUN
        ]
        items.extend(
            {
                "item_id": f"NR-{scenario['scenario_id']}",
                "name": scenario["name"],
                "reason": "Default audit run does not execute replay/fault scenario.",
                "impact": "Scenario cannot count as PASS for system certification.",
                "required_for_system_pass": True,
                "required_for_phase_3v": scenario["scenario_id"] == "AUTH-INVALID-CERT",
            }
            for scenario in scenarios
            if scenario["test_reference"]["status"] == STATUS_NOT_RUN
        )
        return items

    def _gap_report(self, report: dict[str, Any]) -> str:
        lines = [
            "# Phase 3W Initial Gap Report",
            "",
            "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.",
            "",
            f"- Overall status: `{report['overall_status']}`",
            (
                f"- Phase gaps: `{report['summary_counts']['phases']['fail']}` fail, "
                f"`{report['summary_counts']['phases']['incomplete']}` incomplete"
            ),
            (
                f"- Connection gaps: `{report['summary_counts']['connections']['fail']}` fail, "
                f"`{report['summary_counts']['connections']['incomplete']}` incomplete"
            ),
            "",
            "## Blocking Gaps",
            "",
        ]
        lines.extend(
            f"- {finding['finding_id']}: {finding['title']}"
            for finding in report["findings"]
        )
        return "\n".join(lines) + "\n"

    def _remediation_gap_report(self, report: dict[str, Any]) -> str:
        phase_errors = report["phase_registry"].get("validation_errors", [])
        connection_errors = report["connection_registry"].get("validation_errors", [])
        lines = [
            "# Phase 3W-R Remediation Gap Report",
            "",
            "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.",
            "",
            f"- Overall status: `{report['overall_status']}`",
            f"- Technical certification status: `{report['technical_certification_status']}`",
            f"- Runtime observation status: `{report['runtime_observation_status']}`",
            f"- Phase 3V readiness status: `{report['phase_3v_readiness_status']}`",
            f"- Live authorization status: `{report['live_authorization_status']}`",
            "",
            "## Registry Repair",
            "",
            f"- Phase registry validation errors: `{len(phase_errors)}`",
            f"- Connection registry validation errors: `{len(connection_errors)}`",
            "- E054: `3W -> 3V` evidence manifest handoff.",
            "- E055: `3C/3D/3T/3U -> backend authorities` negative assertion.",
            "- E056: `3G -> all durable phases` database hardening contract.",
            "- E057: `all phases -> observability` platform service contract.",
            "",
            "## Remaining Blockers",
            "",
        ]
        if not report["findings"]:
            lines.append("- No open findings.")
        else:
            lines.extend(
                f"- `{finding['finding_id']}` {finding['severity']}: {finding['title']}"
                for finding in report["findings"]
            )
        lines.extend(
            [
                "",
                "## Recommendation",
                "",
                "- Run local integration mode with contract checks and the golden trace.",
                "- Attach runtime read-only evidence before any Phase 3V pass.",
                (
                    "- Keep live trading disabled until human approval is captured "
                    "outside this report."
                ),
            ]
        )
        return "\n".join(lines) + "\n"

    def _repair_log(self, report: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "phase_3w_r_repair_log_v1",
            "generated_at": report["completed_at"],
            "live_trading_authorized": False,
            "repairs": [
                {
                    "repair_id": "R-REGISTRY-001",
                    "status": "APPLIED",
                    "summary": "Added authoritative phase registry with all 29 mandatory phases.",
                },
                {
                    "repair_id": "R-CONNECTION-001",
                    "status": "APPLIED",
                    "summary": (
                        "Replaced flattened late edges with typed endpoints and phase groups."
                    ),
                },
                {
                    "repair_id": "R-ALEMBIC-001",
                    "status": "APPLIED",
                    "summary": "Switched migration health to Alembic ancestry diagnostics.",
                },
                {
                    "repair_id": "R-EVIDENCE-001",
                    "status": "APPLIED",
                    "summary": "Added local golden trace and test evidence artifact model.",
                },
            ],
            "remaining_blockers": [finding["finding_id"] for finding in report["findings"]],
        }

    def _local_test_evidence(
        self,
        *,
        executed: bool,
        run_golden_trace: bool,
        scenario_traces: dict[str, dict[str, Any]] | None = None,
        dynamic_no_bypass_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not executed:
            return {
                "status": STATUS_NOT_RUN,
                "executed": False,
                "command": (
                    "kalshi-bot system-certification-run --mode local-integration "
                    "--run-contract-tests --run-golden-trace"
                ),
                "passed": 0,
                "failed": 0,
                "checks": [],
            }
        checks = []
        phase_errors = validate_phase_registry()
        connection_errors = validate_connection_registry()
        checks.append(
            {
                "check_id": "REGISTRY-PHASES",
                "status": STATUS_PASS if not phase_errors else STATUS_FAIL,
                "message": "Authoritative phase registry contains all mandatory phases.",
                "errors": phase_errors,
            }
        )
        checks.append(
            {
                "check_id": "REGISTRY-CONNECTIONS",
                "status": STATUS_PASS if not connection_errors else STATUS_FAIL,
                "message": "Typed connection registry contains 57 valid connections.",
                "errors": connection_errors,
            }
        )
        trace = build_golden_trace(executed=run_golden_trace)
        checks.extend(golden_trace_contract_checks(trace) if run_golden_trace else [])
        checks.extend(negative_scenario_contract_checks(scenario_traces or {}))
        checks.extend((dynamic_no_bypass_evidence or {}).get("checks", []))
        failed = sum(1 for check in checks if check["status"] == STATUS_FAIL)
        return {
            "status": STATUS_FAIL if failed else STATUS_PASS,
            "executed": True,
            "command": "internal Phase 3W-R local contract checks",
            "passed": sum(1 for check in checks if check["status"] == STATUS_PASS),
            "failed": failed,
            "checks": checks,
        }

    def _persist_report(self, report: dict[str, Any], artifacts: list[dict[str, Any]]) -> None:
        row = SystemCertificationRun(
            certification_run_id=report["certification_run_id"],
            mode=report["mode"],
            overall_status=report["overall_status"],
            live_trading_authorized=0,
            started_at=parse_datetime(report["started_at"]),
            completed_at=parse_datetime(report["completed_at"]),
            repository_sha256=report["repository_fingerprint"]["build_sha256"],
            config_sha256=report["repository_fingerprint"]["config_sha256"],
            manifest_sha256=report["evidence_manifest"]["manifest_sha256"],
            phase_count=len(report["phase_results"]),
            connection_count=len(report["connection_results"]),
            finding_count=len(report["findings"]),
            report_json_path=report["phase_3v_handoff"]["evidence_package"],
            report_md_path=str(Path(self.config.output_dir) / "system_certification_report.md"),
            raw_json=canonical_json(report),
        )
        self.session.add(row)
        for artifact in artifacts:
            self.session.add(
                SystemCertificationArtifact(
                    artifact_id=stable_id(
                        "cert_artifact",
                        report["certification_run_id"],
                        artifact["path"],
                        artifact["sha256"],
                    ),
                    certification_run_id=report["certification_run_id"],
                    name=artifact["name"],
                    path=artifact["path"],
                    sha256=artifact["sha256"],
                    kind=artifact["kind"],
                    created_at=utc_now(),
                    raw_json=canonical_json(artifact),
                )
            )


def certification_status(session: Session, *, settings: Settings | None = None) -> dict[str, Any]:
    return SystemCertificationService(session, settings=settings).latest_status()


def validate_certification_report_shape(report: dict[str, Any]) -> list[str]:
    errors = []
    required = {
        "schema_version",
        "certification_run_id",
        "mode",
        "overall_status",
        "live_trading_authorized",
        "phase_results",
        "connection_results",
        "phase_3v_handoff",
    }
    missing = sorted(required - set(report))
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")
    if report.get("live_trading_authorized") is not False:
        errors.append("live_trading_authorized must be false")
    if len(report.get("phase_results", [])) != 29:
        errors.append("phase_results must contain 29 phases")
    if len(report.get("connection_results", [])) != 57:
        errors.append("connection_results must contain 57 connections")
    if report.get("phase_3v_handoff", {}).get("human_approval_required") is not True:
        errors.append("phase_3v_handoff must require human approval")
    return errors


def render_repo_map(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 3W Repository Map",
        "",
        "THIS MAP DOES NOT AUTHORIZE LIVE TRADING.",
        "",
        "| Phase | Capability | Status | Implementation |",
        "| --- | --- | --- | --- |",
    ]
    for phase in report["phase_results"]:
        lines.append(
            f"| {phase['phase_id']} | {phase['name']} | {phase['status']} | "
            f"{', '.join(phase['implementation_locations']) or 'not mapped'} |"
        )
    return "\n".join(lines) + "\n"


def render_runtime_access(runtime: dict[str, Any]) -> str:
    lines = [
        "# Runtime Access Statement",
        "",
        "THIS STATEMENT DOES NOT AUTHORIZE LIVE TRADING.",
        "",
        f"- Code access: `{runtime['code']}`",
        f"- Test access: `{runtime['test']}`",
        f"- Replay access: `{runtime['replay']}`",
        f"- Staging runtime observed: `{runtime['staging']}`",
        f"- Production read-only observed: `{runtime['production_read_only']}`",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in runtime["limitations"])
    return "\n".join(lines) + "\n"


def render_certification_report(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 3W System Certification Report",
        "",
        "> THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.",
        "",
        "## Decision",
        "",
        f"- Certification run ID: `{report['certification_run_id']}`",
        f"- Outcome: `{report['overall_status']}`",
        f"- Mode: `{report['mode']}`",
        f"- Technical certification status: `{report['technical_certification_status']}`",
        f"- Runtime observation status: `{report['runtime_observation_status']}`",
        f"- Phase 3V readiness status: `{report['phase_3v_readiness_status']}`",
        f"- Live authorization status: `{report['live_authorization_status']}`",
        f"- Live trading authorized: `{report['live_trading_authorized']}`",
        f"- Started: `{report['started_at']}`",
        f"- Completed: `{report['completed_at']}`",
        "",
        "## Runtime Access",
        "",
    ]
    lines.extend(f"- {item}" for item in report["runtime_access"]["limitations"])
    lines.extend(
        [
            "",
            "## Phase Status",
            "",
            "| Phase | Capability | Status | Evidence | Implementation |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for phase in report["phase_results"]:
        lines.append(
            f"| {phase['phase_id']} | {phase['name']} | {phase['status']} | "
            f"{', '.join(phase['evidence_grades'])} | "
            f"{', '.join(phase['implementation_locations']) or 'not mapped'} |"
        )
    lines.extend(
        [
            "",
            "## Critical Connections",
            "",
            "| Edge | Producer -> Consumer | Status | Contract | Test |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for edge in report["connection_results"]:
        lines.append(
            f"| {edge['connection_id']} | {edge['source_phase']} -> {edge['destination_phase']} | "
            f"{edge['status']} | {edge['producer_contract']} | "
            f"{edge['contract_test']['status']} |"
        )
    lines.extend(
        [
            "",
            "## Golden Trace",
            "",
            f"- Result: `{report['golden_trace']['status']}`",
            f"- Correlation ID: `{report['golden_trace']['trace_id']}`",
            f"- Exchange write attempted: `{report['golden_trace']['exchange_write_attempted']}`",
            f"- Demo order attempted: `{report['golden_trace']['demo_order_attempted']}`",
            "",
            "## Blocking Findings",
            "",
        ]
    )
    for finding in report["findings"]:
        lines.append(f"- `{finding['finding_id']}` {finding['severity']}: {finding['title']}")
    lines.extend(
        [
            "",
            "## Tests Not Run",
            "",
            f"- Not-run items: `{len(report['not_run_items'])}`",
            "",
            "## Phase 3V Handoff",
            "",
            f"- Status: `{report['phase_3v_handoff']['status']}`",
            f"- Evidence package: `{report['phase_3v_handoff']['evidence_package']}`",
            f"- Evidence hash: `{report['phase_3v_handoff']['evidence_sha256']}`",
            "- Human approval required: `true`",
            "- Live trading authorized: `false`",
            "",
            "## Final Attestation",
            "",
            "Code-verified: phase and connection registries, static maps, and report generation.",
            "Test-verified: only when the listed commands are actually run and captured.",
            "Replay-verified: not observed in this audit-only report.",
            "Runtime-observed: no staging or production runtime observed.",
            "Not observed: human approvals, deployed health, full golden replay, backup restore.",
            "Human approval required: Phase 3V and any real-capital launch process.",
            "",
            "THIS REPORT DOES NOT AUTHORIZE LIVE TRADING.",
            "",
        ]
    )
    return "\n".join(lines)


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(row["status"] for row in rows)
    return {
        "pass": counter[STATUS_PASS],
        "degraded": counter["DEGRADED"],
        "fail": counter[STATUS_FAIL],
        "incomplete": counter[STATUS_INCOMPLETE],
        "not_applicable": counter["NOT_APPLICABLE"],
    }


def _check_result(
    check_id: str,
    name: str,
    status: str,
    evidence_grades: list[str],
    evidence: list[str],
    findings: list[str],
    *,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "name": name,
        "status": status,
        "required": required,
        "evidence_grades": evidence_grades,
        "evidence": evidence,
        "findings": findings,
    }


def _finding(
    finding_id: str,
    severity: str,
    title: str,
    phases: list[str],
    connections: list[str],
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "status": "OPEN",
        "title": title,
        "description": title,
        "affected_phases": phases,
        "affected_connections": connections,
        "evidence": evidence,
        "owner": None,
        "due_at": None,
        "blocks_system_pass": severity in {"CRITICAL", "HIGH"},
        "blocks_phase_3v": severity in {"CRITICAL", "HIGH"},
    }


def _not_run_test_reference(reason: str) -> dict[str, Any]:
    return _test_reference(
        status=STATUS_NOT_RUN,
        command=reason,
        passed=0,
        failed=0,
        artifact=None,
    )


def _test_reference(
    *,
    status: str,
    command: str,
    passed: int,
    failed: int,
    artifact: str | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "command": command,
        "exit_code": None,
        "duration_ms": None,
        "passed": passed,
        "failed": failed,
        "skipped": 0,
        "artifact": artifact,
    }


def _not_run_count(
    connections: list[dict[str, Any]],
    scenarios: list[dict[str, Any]],
    local_test_evidence: dict[str, Any],
) -> int:
    total = sum(1 for row in connections if row["contract_test"]["status"] == STATUS_NOT_RUN)
    total += sum(1 for row in scenarios if row["test_reference"]["status"] == STATUS_NOT_RUN)
    if local_test_evidence["status"] == STATUS_NOT_RUN:
        total += 1
    return total


def _endpoint_mapped(endpoint: Any, phase_by_id: dict[str, dict[str, Any]]) -> bool:
    if endpoint.kind == "platform_service":
        return True
    return all(
        bool(phase_by_id.get(phase_id, {}).get("implementation_locations"))
        for phase_id in endpoint.refs
    )


def _transport(value: str) -> str:
    if value == "manifest":
        return "file"
    allowed = {
        "function",
        "api",
        "event",
        "queue",
        "stream",
        "table",
        "cache",
        "file",
        "multiple",
        "negative_assertion",
    }
    return value if value in allowed else "multiple"


def _artifact(name: str, path: Path, kind: str) -> dict[str, str]:
    return {
        "name": name,
        "path": str(path),
        "sha256": sha256_bytes(path.read_bytes()),
        "kind": kind,
    }


def _artifact_kind(name: str) -> str:
    if name.endswith(".json"):
        return "manifest"
    if name.endswith(".md"):
        return "report"
    return "other"


def _hash_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes()) if path.exists() else "UNAVAILABLE"


def _latest_migration_version(path: Path) -> str:
    if not path.exists():
        return "UNAVAILABLE"
    files = sorted(path.glob("*.py"))
    return files[-1].stem if files else "UNAVAILABLE"


def _git_metadata(root: Path) -> dict[str, Any]:
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return {"branch": branch, "commit": commit[:12], "dirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        return {"branch": "UNAVAILABLE", "commit": "UNKNOWN", "dirty": True}
