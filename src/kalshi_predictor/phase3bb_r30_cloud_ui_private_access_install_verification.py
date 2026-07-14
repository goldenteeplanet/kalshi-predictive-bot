from __future__ import annotations

import csv
import ipaddress
import json
import time
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
from kalshi_predictor.phase3bb_r12_cloud_bootstrap import (
    ProbeRunner,
    RemoteProbe,
    RemoteProbeResult,
    _result_payload,
    _run_ssh_probe,
)
from kalshi_predictor.phase3bb_r18_cloud_scheduler_runtime_cutover import (
    DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    DEFAULT_REPORTS_DIR,
    build_phase3bb_r18_cloud_scheduler_runtime_cutover,
)
from kalshi_predictor.phase3bb_r20_cloud_ui_service_plan import (
    DEFAULT_UI_PORT,
    DEFAULT_UI_SERVICE_NAME,
    _build_ui_probe_commands,
    _parse_ui_probe_results,
    _target_from_payload,
)
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R30_VERSION = "phase3bb_r30_cloud_ui_private_access_install_verification_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r30")
READY_R29_STATUS = "HANDOFF_READY_PRIVATE_ACCESS_INSTALL_DRY_RUN"


@dataclass(frozen=True)
class Phase3BBR30CloudUiPrivateAccessInstallVerificationArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    probe_csv_path: Path
    checks_csv_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r30_cloud_ui_private_access_install_verification_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
) -> Phase3BBR30CloudUiPrivateAccessInstallVerificationArtifacts:
    payload = build_phase3bb_r30_cloud_ui_private_access_install_verification(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=probe_runner,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_private_access_install_verification.md"
    json_path = output_dir / "cloud_ui_private_access_install_verification.json"
    probe_csv_path = output_dir / "remote_private_access_probe_results.csv"
    checks_csv_path = output_dir / "private_access_verification_checks.csv"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_probe_csv(probe_csv_path, payload["remote_private_access_probe_results"])
    _write_checks_csv(checks_csv_path, payload["verification_checks"])
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            probe_csv_path,
            checks_csv_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR30CloudUiPrivateAccessInstallVerificationArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        probe_csv_path=probe_csv_path,
        checks_csv_path=checks_csv_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r30_cloud_ui_private_access_install_verification(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    ssh_target: str | None = None,
    identity_file: str | None = None,
    app_path: str | None = None,
    env_path: str | None = None,
    db_path: str | None = None,
    ui_service_name: str = DEFAULT_UI_SERVICE_NAME,
    ui_port: int = DEFAULT_UI_PORT,
    per_probe_timeout_seconds: int = DEFAULT_PER_PROBE_TIMEOUT_SECONDS,
    probe_runner: ProbeRunner | None = None,
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
        "command": "kalshi-bot phase3bb-r30-cloud-ui-private-access-install-verification",
        "argv": command_args or [],
    }
    r29_path = reports_dir / "phase3bb_r29" / "cloud_ui_private_access_install_handoff.json"
    r29 = _read_json(r29_path)
    runner = probe_runner or _run_ssh_probe
    r18 = build_phase3bb_r18_cloud_scheduler_runtime_cutover(
        session,
        output_dir=output_dir / "r18_preflight",
        reports_dir=reports_dir,
        settings=resolved,
        command_args=["phase3bb-r18-cloud-scheduler-runtime-cutover"],
        ssh_target=ssh_target,
        identity_file=identity_file,
        app_path=app_path,
        env_path=env_path,
        db_path=db_path,
        per_probe_timeout_seconds=per_probe_timeout_seconds,
        probe_runner=runner,
    )
    target = _target_from_payload(dict(r18.get("cloud_target") or {}))
    private_probes = _build_private_access_probe_commands(
        ui_port=ui_port,
        timeout_seconds=per_probe_timeout_seconds,
    )
    ui_probes = _build_ui_probe_commands(
        ui_service_name=ui_service_name,
        ui_port=ui_port,
        timeout_seconds=per_probe_timeout_seconds,
    )
    started = time.monotonic()
    private_results = [runner(probe, target) for probe in private_probes]
    ui_results = [runner(probe, target) for probe in ui_probes]
    duration = round(time.monotonic() - started, 3)
    tailscale_state = _parse_private_access_probe_results(private_results, ui_port=ui_port)
    ui_state = _parse_ui_probe_results(ui_results, ui_service_name=ui_service_name)
    ui_state = _normalize_private_access_ui_state(ui_state, tailscale_state=tailscale_state)
    checks = _verification_checks(
        r18=r18,
        r29=r29,
        tailscale_state=tailscale_state,
        ui_state=ui_state,
    )
    decision = _verification_decision(
        checks,
        r18=r18,
        r29=r29,
        tailscale_state=tailscale_state,
        ui_state=ui_state,
    )
    all_results = private_results + ui_results
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "post_operator_verification_only": True,
        "remote_commands_executed": len(all_results),
        "remote_report_writes_only": True,
        "remote_db_writes_performed": 0,
        "service_files_written_to_system": False,
        "systemctl_read_only_commands_executed": 4,
        "systemctl_mutating_commands_executed": 0,
        "tailscale_read_only_commands_executed": 4,
        "tailscale_mutating_commands_executed": 0,
        "ssh_commands_execute_read_only_probes": len(all_results),
        "nginx_or_firewall_changed": False,
        "public_exposure_changed": False,
        "secrets_printed": False,
        "secrets_copied": False,
        "starts_ui_service": False,
        "starts_r5_watcher": False,
        "starts_duplicate_watchers": False,
        "stops_processes": False,
        "creates_paper_trades": False,
        "creates_paper_orders": False,
        "places_exchange_orders": False,
        "submits_cancels_replaces_orders": False,
        "db_writes_performed": 0,
    }
    return {
        **metadata,
        "phase": "3BB-R30-CLOUD-UI-PRIVATE-ACCESS-INSTALL-VERIFICATION",
        "phase_version": PHASE3BB_R30_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_PRIVATE_ACCESS_INSTALL_VERIFICATION",
        "reports_dir": str(reports_dir),
        "r29_artifact_path": str(r29_path),
        "r29_context_available": bool(r29),
        "r18_preflight": r18,
        "remote_private_access_probe_duration_seconds": duration,
        "remote_private_access_probe_results": [
            _result_payload(result) for result in all_results
        ],
        "parsed_private_access_state": tailscale_state,
        "parsed_ui_state": ui_state,
        "private_access_handoff_decision": r29.get("private_access_handoff_decision")
        or {},
        "verification_checks": checks,
        "verification_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _build_private_access_probe_commands(
    *,
    ui_port: int,
    timeout_seconds: int,
) -> list[RemoteProbe]:
    return [
        RemoteProbe("tailscale_binary", "command -v tailscale || true", timeout_seconds),
        RemoteProbe(
            "tailscaled_unit",
            (
                "systemctl show tailscaled --no-pager -p LoadState -p UnitFileState "
                "-p ActiveState -p SubState -p ExecMainPID || true"
            ),
            timeout_seconds,
        ),
        RemoteProbe(
            "tailscale_status",
            "tailscale status --json 2>/dev/null || tailscale status 2>&1 || true",
            timeout_seconds,
        ),
        RemoteProbe("tailscale_ip", "tailscale ip -4 2>/dev/null || true", timeout_seconds),
        RemoteProbe(
            "tailscale_serve_status",
            "sudo -n tailscale serve status 2>/dev/null || tailscale serve status 2>&1 || true",
            timeout_seconds,
        ),
        RemoteProbe(
            "tailscale_local_backend_probe",
            f"curl -fsS -m 5 http://127.0.0.1:{ui_port}/ >/tmp/phase3bb_r30_ui.html "
            "&& echo HTTP_OK || echo HTTP_NOT_READY",
            timeout_seconds,
        ),
    ]


def _verification_checks(
    *,
    r18: dict[str, Any],
    r29: dict[str, Any],
    tailscale_state: dict[str, Any],
    ui_state: dict[str, Any],
) -> list[dict[str, Any]]:
    r18_decision = r18.get("runtime_cutover_decision") or {}
    r29_decision = r29.get("private_access_handoff_decision") or {}
    return [
        _check("r29_handoff_artifact_present", bool(r29), "R29 handoff artifact exists."),
        _check(
            "r29_handoff_ready",
            r29_decision.get("status") == READY_R29_STATUS,
            f"R29 status is {r29_decision.get('status')}.",
        ),
        _check(
            "r18_systemd_owns_r5",
            r18_decision.get("status") == "SYSTEMD_OWNS_R5",
            f"R18 status is {r18_decision.get('status')}.",
        ),
        _check(
            "no_duplicate_r5",
            not bool(r18_decision.get("duplicate_r5")),
            f"duplicate_r5={r18_decision.get('duplicate_r5')}.",
        ),
        _check(
            "ui_service_still_running",
            bool(ui_state.get("service_started")),
            f"UI ActiveState={ui_state.get('service_active_state')}.",
        ),
        _check(
            "ui_listener_localhost_only",
            bool(ui_state.get("ui_port_listening"))
            and "127.0.0.1:8080" in str(ui_state.get("listener_text") or "")
            and "0.0.0.0:8080" not in str(ui_state.get("listener_text") or ""),
            f"Listeners: {ui_state.get('listener_text') or 'none'}.",
        ),
        _check(
            "no_public_http_https_exposure",
            not bool(ui_state.get("public_http_listening"))
            and not bool(ui_state.get("public_https_listening")),
            f"Listeners: {ui_state.get('listener_text') or 'none'}.",
        ),
        _check(
            "local_backend_http_ok",
            bool(ui_state.get("local_ui_http_ok"))
            and bool(tailscale_state.get("local_backend_http_ok")),
            f"UI local={ui_state.get('local_ui_http_ok')}; "
            f"Tailscale probe backend={tailscale_state.get('local_backend_http_ok')}.",
        ),
        _check(
            "tailscale_installed",
            bool(tailscale_state.get("tailscale_installed")),
            f"tailscale_path={tailscale_state.get('tailscale_path')}.",
        ),
        _check(
            "tailscaled_service_active",
            bool(tailscale_state.get("tailscaled_active")),
            f"tailscaled ActiveState={tailscale_state.get('tailscaled_active_state')}.",
        ),
        _check(
            "tailscale_authenticated",
            bool(tailscale_state.get("tailscale_authenticated")),
            f"backend_state={tailscale_state.get('backend_state')}; "
            f"login_required={tailscale_state.get('login_required')}.",
        ),
        _check(
            "tailscale_ip_present",
            bool(tailscale_state.get("tailnet_ipv4")),
            f"tailnet_ipv4={tailscale_state.get('tailnet_ipv4')}.",
        ),
        _check(
            "tailscale_serve_configured",
            bool(tailscale_state.get("serve_configured")),
            f"serve_status={tailscale_state.get('serve_status_excerpt') or 'none'}.",
        ),
        _check(
            "tailscale_serve_targets_localhost_ui",
            bool(tailscale_state.get("serve_targets_localhost_ui")),
            "serve target mentions localhost UI="
            f"{tailscale_state.get('serve_targets_localhost_ui')}.",
        ),
        _check(
            "tailscale_funnel_not_enabled",
            not bool(tailscale_state.get("funnel_enabled")),
            f"funnel_enabled={tailscale_state.get('funnel_enabled')}.",
        ),
    ]


def _verification_decision(
    checks: list[dict[str, Any]],
    *,
    r18: dict[str, Any],
    r29: dict[str, Any],
    tailscale_state: dict[str, Any],
    ui_state: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r18_decision = r18.get("runtime_cutover_decision") or {}
    r29_decision = r29.get("private_access_handoff_decision") or {}
    if failed:
        status = "PRIVATE_ACCESS_INSTALL_NOT_VERIFIED"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R30 - Resolve Cloud UI Private Access Verification"
        next_command = _blocked_next_command(failed[0]["check"])
    else:
        status = "VERIFIED_PRIVATE_ACCESS_UI_READY"
        reason = (
            "Tailscale is installed, authenticated, has a tailnet IPv4, and serves "
            "the localhost-only cloud UI without public HTTP/HTTPS or Funnel exposure."
        )
        next_step = "Phase 3BB-R31 - Cloud UI Private Access Operator Smoke Test"
        next_command = "tailscale status && tailscale serve status"
    return {
        "status": status,
        "verification_passed": not failed,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r18_status": r18_decision.get("status"),
        "r29_status": r29_decision.get("status"),
        "r5_pid": r18_decision.get("current_r5_pid") or r29_decision.get("r5_pid"),
        "ui_service_started": bool(ui_state.get("service_started")),
        "ui_port_listening": bool(ui_state.get("ui_port_listening")),
        "public_http_listening": bool(ui_state.get("public_http_listening")),
        "public_https_listening": bool(ui_state.get("public_https_listening")),
        "tailscale_installed": bool(tailscale_state.get("tailscale_installed")),
        "tailscaled_active": bool(tailscale_state.get("tailscaled_active")),
        "tailscale_authenticated": bool(tailscale_state.get("tailscale_authenticated")),
        "tailnet_ipv4": tailscale_state.get("tailnet_ipv4"),
        "tailscale_serve_configured": bool(tailscale_state.get("serve_configured")),
        "tailscale_funnel_enabled": bool(tailscale_state.get("funnel_enabled")),
        "operator_next_command": next_command,
        "next_codex_step": next_step,
    }


def _blocked_next_command(first_failed_check: str) -> str:
    if first_failed_check in {
        "tailscale_installed",
        "tailscaled_service_active",
        "tailscale_authenticated",
        "tailscale_serve_configured",
        "tailscale_serve_targets_localhost_ui",
    }:
        return (
            "PHASE3BB_R29_EXECUTE=I_APPROVE_R29_PRIVATE_ACCESS_INSTALL "
            "bash reports/phase3bb_r29/operator_private_access_install_handoff.sh"
        )
    return (
        "kalshi-bot phase3bb-r30-cloud-ui-private-access-install-verification "
        "--output-dir reports/phase3bb_r30 --reports-dir reports"
    )


def _normalize_private_access_ui_state(
    ui_state: dict[str, Any],
    *,
    tailscale_state: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(ui_state)
    listener_text = str(normalized.get("listener_text") or "")
    tailnet_ipv4 = tailscale_state.get("tailnet_ipv4")
    normalized["public_http_listening"] = _listener_has_public_port(
        listener_text,
        80,
        tailnet_ipv4=tailnet_ipv4,
    )
    normalized["public_https_listening"] = _listener_has_public_port(
        listener_text,
        443,
        tailnet_ipv4=tailnet_ipv4,
    )
    return normalized


def _listener_has_public_port(
    text: str,
    port: int,
    *,
    tailnet_ipv4: Any,
) -> bool:
    for line in text.splitlines():
        host = _listener_host_for_port(line, port)
        if host is not None and _is_public_listener_host(host, tailnet_ipv4=tailnet_ipv4):
            return True
    return False


def _listener_host_for_port(line: str, port: int) -> str | None:
    suffix = f":{port}"
    for token in line.split():
        cleaned = token.strip()
        if not cleaned.endswith(suffix):
            continue
        host = cleaned[: -len(suffix)]
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        return host
    return None


def _is_public_listener_host(host: str, *, tailnet_ipv4: Any) -> bool:
    stripped = host.strip()
    if stripped in {"", "*", "0.0.0.0", "::", "[::]"}:
        return True
    if tailnet_ipv4 and stripped == str(tailnet_ipv4):
        return False
    try:
        address = ipaddress.ip_address(stripped)
    except ValueError:
        return True
    if address.is_loopback or address.is_private or address.is_link_local:
        return False
    if isinstance(address, ipaddress.IPv4Address) and address in ipaddress.ip_network(
        "100.64.0.0/10"
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address in ipaddress.ip_network(
        "fc00::/7"
    ):
        return False
    return True


def _parse_private_access_probe_results(
    results: list[RemoteProbeResult],
    *,
    ui_port: int,
) -> dict[str, Any]:
    by_name = {result.name: result for result in results}
    binary_text = _stdout(by_name.get("tailscale_binary")).strip()
    unit = _parse_systemd_show(_stdout(by_name.get("tailscaled_unit")))
    status_text = _stdout(by_name.get("tailscale_status")).strip()
    status_json = _json_from_text(status_text)
    serve_text = _stdout(by_name.get("tailscale_serve_status")).strip()
    ip_text = _stdout(by_name.get("tailscale_ip")).strip()
    backend_state = str(status_json.get("BackendState") or "")
    tailscale_installed = bool(binary_text)
    login_required = _login_required(status_text, backend_state, installed=tailscale_installed)
    authenticated = tailscale_installed and _authenticated(
        status_text,
        status_json,
        backend_state,
        login_required,
    )
    tailnet_ipv4 = _first_ipv4(ip_text)
    serve_lower = serve_text.lower()
    serve_target = f"127.0.0.1:{ui_port}"
    return {
        "tailscale_path": binary_text.splitlines()[0] if binary_text else None,
        "tailscale_installed": tailscale_installed,
        "tailscaled_unit": unit,
        "tailscaled_active_state": unit.get("ActiveState"),
        "tailscaled_active": unit.get("ActiveState") == "active",
        "backend_state": backend_state or None,
        "status_json_available": bool(status_json),
        "login_required": login_required,
        "tailscale_authenticated": authenticated,
        "tailnet_ipv4": tailnet_ipv4,
        "serve_status_excerpt": serve_text[:1000],
        "serve_configured": _serve_configured(serve_text),
        "serve_targets_localhost_ui": serve_target in serve_text,
        "funnel_enabled": "funnel" in serve_lower and "off" not in serve_lower,
        "local_backend_http_ok": "HTTP_OK" in _stdout(
            by_name.get("tailscale_local_backend_probe")
        ),
    }


def _login_required(status_text: str, backend_state: str, *, installed: bool) -> bool:
    if not installed:
        return True
    if backend_state.lower() == "running":
        return False
    lowered = status_text.lower()
    if backend_state.lower() in {"needslogin", "stopped", "notrunning"}:
        return True
    return any(
        marker in lowered
        for marker in (
            "not logged in",
            "logged out",
            "needs login",
            "please run",
            "tailscale up",
        )
    )


def _authenticated(
    status_text: str,
    status_json: dict[str, Any],
    backend_state: str,
    login_required: bool,
) -> bool:
    if login_required:
        return False
    if backend_state.lower() == "running":
        return True
    self_state = status_json.get("Self") if isinstance(status_json, dict) else {}
    if isinstance(self_state, dict) and self_state.get("Online") is True:
        return True
    lowered = status_text.lower()
    return bool(status_text.strip()) and "tailscale is stopped" not in lowered


def _serve_configured(text: str) -> bool:
    lowered = text.lower()
    if not text.strip():
        return False
    if any(marker in lowered for marker in ("not configured", "no serve config")):
        return False
    return "127.0.0.1:8080" in text or "serve" in lowered


def _json_from_text(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start >= 0:
        try:
            parsed, _end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(parsed, dict):
            return parsed
        start = text.find("{", start + 1)
    return {}


def _parse_systemd_show(text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _first_ipv4(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.count(".") == 3:
            return stripped
    return None


def _stdout(result: RemoteProbeResult | None) -> str:
    if result is None:
        return ""
    return result.stdout or ""


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R30 Private Access Verification")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Verification passed: `{decision['verification_passed']}`",
            f"- R18 status: `{decision['r18_status']}`",
            f"- R29 status: `{decision['r29_status']}`",
            f"- R5 PID: `{decision['r5_pid']}`",
            f"- UI service started: `{decision['ui_service_started']}`",
            f"- UI port listening: `{decision['ui_port_listening']}`",
            "- Public HTTP/HTTPS listening: "
            f"`{decision['public_http_listening']}` / "
            f"`{decision['public_https_listening']}`",
            f"- Tailscale installed: `{decision['tailscale_installed']}`",
            f"- Tailscaled active: `{decision['tailscaled_active']}`",
            f"- Tailscale authenticated: `{decision['tailscale_authenticated']}`",
            f"- Tailnet IPv4: `{decision['tailnet_ipv4']}`",
            f"- Tailscale Serve configured: `{decision['tailscale_serve_configured']}`",
            f"- Tailscale Funnel enabled: `{decision['tailscale_funnel_enabled']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Codex did not install Tailscale or run `tailscale up`.",
            "- Codex did not start or stop UI/R5 services.",
            "- Codex did not install nginx or open firewall ports.",
            "- No paper/live/demo trades were created.",
            "",
            "## Next Operator Command",
            "",
            f"```bash\n{decision['operator_next_command']}\n```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R30 Verification Detail")
    decision = payload["verification_decision"]
    lines.extend(["", f"- Decision: `{decision['status']}`", "", "## Checks", ""])
    for row in payload["verification_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(
        [
            "",
            "## Parsed Private Access State",
            "",
            "```json",
            json.dumps(payload["parsed_private_access_state"], indent=2, sort_keys=True),
            "```",
            "",
            "## Parsed UI State",
            "",
            "```json",
            json.dumps(payload["parsed_ui_state"], indent=2, sort_keys=True),
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    decision = payload["verification_decision"]
    return "\n".join(
        ["#!/usr/bin/env bash", "set -euo pipefail", "", decision["operator_next_command"], ""]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R30 Next Actions")
    decision = payload["verification_decision"]
    lines.extend(
        [
            "",
            "## Next Operator Action",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "```bash",
            decision["operator_next_command"],
            "```",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
            "",
            "## Do Not Run",
            "",
            "- Do not expose the UI publicly.",
            "- Do not install nginx or open firewall ports.",
            "- Do not stop or duplicate R5.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_probe_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_checks_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["check", "passed", "detail"],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
