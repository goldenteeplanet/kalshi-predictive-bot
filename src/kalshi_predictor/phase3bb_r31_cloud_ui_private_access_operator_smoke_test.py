from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.phase3bb_acceleration import (
    _metadata,
    _metadata_lines,
    _read_json,
    _safety_flags,
    _write_manifest,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import DEFAULT_REPORTS_DIR
from kalshi_predictor.phase3bb_r25_cloud_ui_operator_smoke_test import (
    LocalProbeRunner,
    _check,
    _default_smoke_probes,
    _mark_executable,
    _result_payload,
    _route_check,
    _run_local_http_probe,
    _shell_quote,
    _write_checks_csv,
    _write_results_csv,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R31_VERSION = "phase3bb_r31_cloud_ui_private_access_operator_smoke_test_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r31")
VERIFIED_R30_STATUS = "VERIFIED_PRIVATE_ACCESS_UI_READY"
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Phase3BBR31CloudUiPrivateAccessOperatorSmokeTestArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    results_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r31_cloud_ui_private_access_operator_smoke_test_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_runner: LocalProbeRunner | None = None,
) -> Phase3BBR31CloudUiPrivateAccessOperatorSmokeTestArtifacts:
    payload = build_phase3bb_r31_cloud_ui_private_access_operator_smoke_test(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        private_base_url=private_base_url,
        timeout_seconds=timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_private_access_operator_smoke_test.md"
    json_path = output_dir / "cloud_ui_private_access_operator_smoke_test.json"
    results_csv_path = output_dir / "private_access_ui_smoke_results.csv"
    checks_csv_path = output_dir / "smoke_checks.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_results_csv(results_csv_path, payload["private_access_ui_smoke_results"])
    _write_checks_csv(checks_csv_path, payload["smoke_checks"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            results_csv_path,
            checks_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR31CloudUiPrivateAccessOperatorSmokeTestArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        results_csv_path=results_csv_path,
        checks_csv_path=checks_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r31_cloud_ui_private_access_operator_smoke_test(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    private_base_url: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    probe_runner: LocalProbeRunner | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    now = utc_now()
    metadata = _metadata(
        session,
        settings=resolved,
        generated_at=now.isoformat(),
        command_args=command_args or [],
        output_dir=output_dir,
    )
    metadata["command_arguments"] = {
        "command": "kalshi-bot phase3bb-r31-cloud-ui-private-access-operator-smoke-test",
        "argv": command_args or [],
    }
    r30_path = reports_dir / "phase3bb_r30" / "cloud_ui_private_access_install_verification.json"
    r30 = _read_json(r30_path)
    resolved_url = _resolve_private_base_url(private_base_url, r30)
    probes = _default_smoke_probes(timeout_seconds=timeout_seconds)
    runner = probe_runner or _run_local_http_probe
    results = [runner(probe, resolved_url) for probe in probes] if resolved_url else []
    result_payloads = [_result_payload(result) for result in results]
    prechecks = _prechecks(r30, resolved_url)
    route_checks = [
        _route_check(probe, result)
        for probe, result in zip(probes, results, strict=True)
    ]
    checks = prechecks + route_checks
    decision = _decision(checks, r30, result_payloads, resolved_url)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "private_tailnet_http_read_only_smoke": True,
        "local_http_requests_executed": len(results),
        "http_get_requests": len(results),
        "remote_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
        "service_files_written_to_system": False,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_mutating_commands_executed": 0,
        "starts_ui_service": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "nginx_or_firewall_changed": False,
        "public_exposure_changed": False,
        "secrets_printed": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
    }
    return {
        **metadata,
        "phase": "3BB-R31-CLOUD-UI-PRIVATE-ACCESS-OPERATOR-SMOKE-TEST",
        "phase_version": PHASE3BB_R31_VERSION,
        "mode": "PAPER_READ_ONLY_TAILSCALE_UI_OPERATOR_SMOKE_TEST",
        "reports_dir": str(reports_dir),
        "r30_artifact_path": str(r30_path),
        "r30_context_available": bool(r30),
        "private_base_url": resolved_url,
        "private_access_ui_smoke_results": result_payloads,
        "smoke_checks": checks,
        "smoke_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _resolve_private_base_url(private_base_url: str | None, r30: dict[str, Any]) -> str:
    if private_base_url:
        return private_base_url.rstrip("/")
    private_state = r30.get("parsed_private_access_state") or {}
    excerpt = str(private_state.get("serve_status_excerpt") or "")
    match = re.search(r"https://[^\s()]+", excerpt)
    if match:
        return match.group(0).rstrip("/")
    return ""


def _prechecks(r30: dict[str, Any], private_base_url: str) -> list[dict[str, Any]]:
    r30_decision = r30.get("verification_decision") or {}
    return [
        _check("r30_artifact_present", bool(r30), "R30 private access verification exists."),
        _check(
            "r30_private_access_verified",
            r30_decision.get("status") == VERIFIED_R30_STATUS,
            f"R30 status is {r30_decision.get('status')}.",
        ),
        _check(
            "private_base_url_present",
            bool(private_base_url),
            f"Private base URL is {private_base_url or 'missing'}.",
        ),
        _check(
            "private_base_url_is_tailscale_https",
            _is_tailscale_https_url(private_base_url),
            f"Private base URL is {private_base_url or 'missing'}.",
        ),
        _check(
            "r30_no_public_exposure",
            not bool(r30_decision.get("public_http_listening"))
            and not bool(r30_decision.get("public_https_listening"))
            and not bool(r30_decision.get("tailscale_funnel_enabled")),
            "R30 public exposure and Funnel flags remain false.",
        ),
    ]


def _decision(
    checks: list[dict[str, Any]],
    r30: dict[str, Any],
    results: list[dict[str, Any]],
    private_base_url: str,
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    route_failed = [row for row in failed if row["check"].startswith("route_")]
    connected = any(row.get("status_code") == 200 for row in results)
    r30_decision = r30.get("verification_decision") or {}
    if failed:
        if not connected:
            status = "BLOCKED_PRIVATE_ACCESS_URL_NOT_REACHABLE"
            reason = "No smoke route returned HTTP 200 through the private Tailscale URL."
            next_step = "Phase 3BB-R31 - Resolve Private Access Smoke Reachability"
        else:
            status = "BLOCKED_PRIVATE_ACCESS_OPERATOR_SMOKE_TEST"
            reason = f"First failing check: {failed[0]['check']}."
            next_step = "Phase 3BB-R31 - Fix Private Access UI Smoke Failure"
    else:
        status = "VERIFIED_PRIVATE_ACCESS_OPERATOR_SMOKE_PASS"
        reason = (
            "The operator UI is reachable through the private Tailscale URL and all "
            "read-only smoke probes passed."
        )
        next_step = "Phase 3BB-R32 - Cloud UI Dashboard Truth And Scheduler Status Verification"
    return {
        "status": status,
        "smoke_passed": not failed,
        "failed_check_count": len(failed),
        "failed_route_count": len(route_failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r30_status": r30_decision.get("status"),
        "r5_pid": r30_decision.get("r5_pid"),
        "private_base_url": private_base_url,
        "routes_checked": len(results),
        "routes_http_200": sum(1 for row in results if row.get("status_code") == 200),
        "operator_next_command": (
            f"Open {private_base_url} in a browser signed into the tailnet."
            if private_base_url
            else (
                "kalshi-bot phase3bb-r30-cloud-ui-private-access-install-verification "
                "--output-dir reports/phase3bb_r30 --reports-dir reports"
            )
        ),
        "next_codex_step": next_step,
    }


def _is_tailscale_https_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    return parsed.scheme == "https" and hostname.endswith(".ts.net")


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R31 Private Access Operator Smoke Test")
    decision = payload["smoke_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Smoke passed: `{decision['smoke_passed']}`",
            f"- Private base URL: `{decision['private_base_url']}`",
            f"- Routes checked: `{decision['routes_checked']}`",
            f"- HTTP 200 routes: `{decision['routes_http_200']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Used the private Tailscale URL only.",
            "- Did not expose the UI publicly.",
            "- Did not start/stop UI or R5.",
            "- Did not create paper/live/demo trades.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R31 Smoke Test Detail")
    decision = payload["smoke_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["smoke_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Routes", ""])
    for row in payload["private_access_ui_smoke_results"]:
        lines.append(
            f"- `{row['name']}` `{row['method']} {row['path']}` "
            f"status=`{row['status_code']}` duration=`{row['duration_seconds']}s`"
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["smoke_decision"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(decision['operator_next_command'])}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R31 Next Actions")
    decision = payload["smoke_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Command/action: `{decision['operator_next_command']}`",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly.",
            "- Do not enable Tailscale Funnel.",
            "- Do not stop or duplicate R5.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"
