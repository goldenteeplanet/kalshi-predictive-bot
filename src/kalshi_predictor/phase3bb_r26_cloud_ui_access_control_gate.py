from __future__ import annotations

import csv
import json
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
from kalshi_predictor.utils.time import utc_now

PHASE3BB_R26_VERSION = "phase3bb_r26_cloud_ui_access_control_gate_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r26")
DEFAULT_MAX_PUBLIC_ROUTE_SECONDS = 10.0
PUBLIC_AUTH_MODES = {"basic_auth", "oauth_proxy", "cloudflare_access", "tailscale_funnel_auth"}


@dataclass(frozen=True)
class Phase3BBR26CloudUiAccessControlGateArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    checks_csv_path: Path
    options_csv_path: Path
    https_draft_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r26_cloud_ui_access_control_gate_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    public_domain: str | None = None,
    operator_ip_cidr: str | None = None,
    auth_mode: str = "none",
    max_public_route_seconds: float = DEFAULT_MAX_PUBLIC_ROUTE_SECONDS,
) -> Phase3BBR26CloudUiAccessControlGateArtifacts:
    payload = build_phase3bb_r26_cloud_ui_access_control_gate(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        public_domain=public_domain,
        operator_ip_cidr=operator_ip_cidr,
        auth_mode=auth_mode,
        max_public_route_seconds=max_public_route_seconds,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_access_control_decision.md"
    json_path = output_dir / "cloud_ui_access_control_decision.json"
    checks_csv_path = output_dir / "access_control_checks.csv"
    options_csv_path = output_dir / "exposure_options.csv"
    https_draft_path = output_dir / "kalshi-ui.https-access.draft"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_csv(checks_csv_path, payload["access_control_checks"], ["check", "passed", "detail"])
    _write_csv(
        options_csv_path,
        payload["exposure_options"],
        ["option", "status", "risk", "operator_action", "notes"],
    )
    https_draft_path.write_text(_render_https_draft(payload), encoding="utf-8")
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            checks_csv_path,
            options_csv_path,
            https_draft_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR26CloudUiAccessControlGateArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        checks_csv_path=checks_csv_path,
        options_csv_path=options_csv_path,
        https_draft_path=https_draft_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r26_cloud_ui_access_control_gate(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    public_domain: str | None = None,
    operator_ip_cidr: str | None = None,
    auth_mode: str = "none",
    max_public_route_seconds: float = DEFAULT_MAX_PUBLIC_ROUTE_SECONDS,
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
        "command": "kalshi-bot phase3bb-r26-cloud-ui-access-control-gate",
        "argv": command_args or [],
    }
    r20_path = reports_dir / "phase3bb_r20" / "cloud_ui_service_plan.json"
    r21_path = reports_dir / "phase3bb_r21" / "cloud_ui_install_review.json"
    r24_path = reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json"
    r25_path = reports_dir / "phase3bb_r25" / "cloud_ui_operator_smoke_test.json"
    r20 = _read_json(r20_path)
    r21 = _read_json(r21_path)
    r24 = _read_json(r24_path)
    r25 = _read_json(r25_path)
    route_timings = _route_timings(r25)
    slow_routes = [
        row
        for row in route_timings
        if float(row.get("duration_seconds") or 0.0) > max_public_route_seconds
    ]
    inputs = {
        "public_domain": (public_domain or "").strip(),
        "operator_ip_cidr": (operator_ip_cidr or "").strip(),
        "auth_mode": (auth_mode or "none").strip().lower(),
        "max_public_route_seconds": max_public_route_seconds,
    }
    checks = _access_checks(
        r20=r20,
        r21=r21,
        r24=r24,
        r25=r25,
        inputs=inputs,
        slow_routes=slow_routes,
    )
    decision = _decision(checks, inputs, route_timings, slow_routes, r24, r25)
    exposure_options = _exposure_options(decision, inputs)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "decision_gate_only": True,
        "no_deploy": True,
        "no_service_install": True,
        "no_service_start": True,
        "no_service_enable": True,
        "no_nginx_install": True,
        "no_firewall_change": True,
        "public_exposure_changed": False,
        "service_files_written_to_system": False,
        "systemctl_commands_executed": 0,
        "ssh_commands_executed": 0,
        "remote_commands_executed": 0,
        "remote_db_writes_performed": 0,
        "db_writes_performed": 0,
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
    }
    return {
        **metadata,
        "phase": "3BB-R26-CLOUD-UI-ACCESS-CONTROL-GATE",
        "phase_version": PHASE3BB_R26_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_ACCESS_CONTROL_DECISION_GATE",
        "reports_dir": str(reports_dir),
        "r20_artifact_path": str(r20_path),
        "r21_artifact_path": str(r21_path),
        "r24_artifact_path": str(r24_path),
        "r25_artifact_path": str(r25_path),
        "input_parameters": inputs,
        "route_timings": route_timings,
        "slow_routes": slow_routes,
        "access_control_checks": checks,
        "exposure_options": exposure_options,
        "access_control_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _access_checks(
    *,
    r20: dict[str, Any],
    r21: dict[str, Any],
    r24: dict[str, Any],
    r25: dict[str, Any],
    inputs: dict[str, Any],
    slow_routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    r20_plan = r20.get("ui_service_plan") or {}
    r21_decision = r21.get("install_review_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    r24_ui_state = r24.get("parsed_ui_state") or {}
    r25_decision = r25.get("smoke_decision") or {}
    auth_mode = str(inputs.get("auth_mode") or "none")
    return [
        _check("r24_tunnel_ready", r24_decision.get("status") == "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY", f"R24 status is {r24_decision.get('status')}."),
        _check("r25_smoke_passed", r25_decision.get("status") == "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS", f"R25 status is {r25_decision.get('status')}."),
        _check("ui_listener_localhost_only", "127.0.0.1:8080" in str(r24_ui_state.get("listener_text") or ""), f"Listener: {r24_ui_state.get('listener_text') or 'unknown'}."),
        _check("no_public_http_listener", r24_decision.get("public_http_listening") is False, f"public_http_listening={r24_decision.get('public_http_listening')}."),
        _check("no_public_https_listener", r24_decision.get("public_https_listening") is False, f"public_https_listening={r24_decision.get('public_https_listening')}."),
        _check("r20_public_exposure_deferred", r20_plan.get("expose_public_allowed_now") is False, f"R20 expose_public_allowed_now={r20_plan.get('expose_public_allowed_now')}."),
        _check("r21_public_exposure_deferred", r21_decision.get("public_exposure_allowed_now") is False, f"R21 public_exposure_allowed_now={r21_decision.get('public_exposure_allowed_now')}."),
        _check("domain_provided_for_public_https", bool(inputs.get("public_domain")), "A DNS name is required before HTTPS exposure review."),
        _check("operator_ip_cidr_provided", bool(inputs.get("operator_ip_cidr")), "An operator IP/CIDR allowlist is required before public exposure review."),
        _check("auth_mode_is_public_safe", auth_mode in PUBLIC_AUTH_MODES, f"auth_mode={auth_mode}; expected one of {', '.join(sorted(PUBLIC_AUTH_MODES))}."),
        _check("no_slow_routes_for_public_exposure", not slow_routes, f"Slow routes over {inputs.get('max_public_route_seconds')}s: {', '.join(row['name'] for row in slow_routes) if slow_routes else 'none'}."),
    ]


def _decision(
    checks: list[dict[str, Any]],
    inputs: dict[str, Any],
    route_timings: list[dict[str, Any]],
    slow_routes: list[dict[str, Any]],
    r24: dict[str, Any],
    r25: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    base_failures = [
        row for row in failed if row["check"] in {"r24_tunnel_ready", "r25_smoke_passed", "ui_listener_localhost_only", "no_public_http_listener", "no_public_https_listener"}
    ]
    public_gates = [
        "domain_provided_for_public_https",
        "operator_ip_cidr_provided",
        "auth_mode_is_public_safe",
        "no_slow_routes_for_public_exposure",
    ]
    public_failures = [row for row in failed if row["check"] in public_gates]
    r24_decision = r24.get("verification_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    if base_failures:
        status = "BLOCKED_UI_NOT_READY_FOR_ACCESS_DECISION"
        reason = f"Base UI/tunnel gate failed: {base_failures[0]['check']}."
        next_step = "Phase 3BB-R24/R25 - Restore Cloud UI Tunnel And Smoke Pass"
        operator_command = (
            "kalshi-bot phase3bb-r25-cloud-ui-operator-smoke-test "
            "--output-dir reports/phase3bb_r25 --reports-dir reports "
            "--local-base-url http://127.0.0.1:8081 --timeout-seconds 60"
        )
    elif public_failures:
        status = "SSH_TUNNEL_APPROVED_PUBLIC_HTTPS_BLOCKED"
        reason = (
            "SSH tunnel access is working and remains the approved path. "
            f"Public HTTPS is blocked by {public_failures[0]['check']}."
        )
        next_step = "Phase 3BB-R27 - Cloud UI Private Access/Auth Draft"
        operator_command = "Open http://127.0.0.1:8081 through the existing SSH tunnel."
    else:
        status = "PUBLIC_HTTPS_REVIEW_READY_NO_INSTALL"
        reason = (
            "All review gates are present for a no-install HTTPS/auth draft. "
            "This phase still did not install nginx, open firewall ports, or expose the UI."
        )
        next_step = "Phase 3BB-R27 - Cloud HTTPS Auth Proxy No-Install Review"
        operator_command = (
            "kalshi-bot phase3bb-r27-cloud-https-auth-proxy-review "
            "--output-dir reports/phase3bb_r27 --reports-dir reports"
        )
    return {
        "status": status,
        "ssh_tunnel_allowed_now": not base_failures,
        "public_https_allowed_now": False,
        "public_https_review_ready": status == "PUBLIC_HTTPS_REVIEW_READY_NO_INSTALL",
        "install_or_firewall_allowed_now": False,
        "requires_operator_domain": bool(not inputs.get("public_domain")),
        "requires_operator_ip_cidr": bool(not inputs.get("operator_ip_cidr")),
        "requires_auth_mode": str(inputs.get("auth_mode") or "none") not in PUBLIC_AUTH_MODES,
        "slow_route_count": len(slow_routes),
        "slowest_route_seconds": max((float(row.get("duration_seconds") or 0.0) for row in route_timings), default=0.0),
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r24_status": r24_decision.get("status"),
        "r25_status": r25_decision.get("status"),
        "r5_pid": r25_decision.get("r5_pid") or r24_decision.get("r5_pid"),
        "operator_next_command": operator_command,
        "next_codex_step": next_step,
    }


def _exposure_options(decision: dict[str, Any], inputs: dict[str, Any]) -> list[dict[str, str]]:
    public_blocker = decision.get("first_failed_check") or "none"
    return [
        {
            "option": "SSH_TUNNEL_ONLY",
            "status": "APPROVED_NOW" if decision["ssh_tunnel_allowed_now"] else "BLOCKED",
            "risk": "LOW",
            "operator_action": "Keep the SSH tunnel open and browse to the local forwarded port.",
            "notes": "No public listener or web auth surface is required.",
        },
        {
            "option": "PRIVATE_VPN_OR_TAILSCALE",
            "status": "GOOD_NEXT_PRIVATE_ACCESS_OPTION",
            "risk": "LOW_TO_MEDIUM",
            "operator_action": "Review a private-network access draft before any install.",
            "notes": "Better always-on ergonomics than SSH tunnels without open public web exposure.",
        },
        {
            "option": "PUBLIC_HTTPS_WITH_IP_ALLOWLIST_AND_AUTH",
            "status": "REVIEW_READY" if decision["public_https_review_ready"] else "BLOCKED",
            "risk": "MEDIUM",
            "operator_action": "Provide domain, operator IP/CIDR, and approved auth mode before proxy review.",
            "notes": f"domain={inputs.get('public_domain') or 'missing'}; ip={inputs.get('operator_ip_cidr') or 'missing'}; auth={inputs.get('auth_mode')}; blocker={public_blocker}",
        },
        {
            "option": "PUBLIC_HTTPS_OPEN_INTERNET_NO_AUTH",
            "status": "REJECTED",
            "risk": "HIGH",
            "operator_action": "Do not run.",
            "notes": "The UI has no built-in login wall and exposes operational state.",
        },
    ]


def _route_timings(r25: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in r25.get("local_ui_smoke_results") or []:
        try:
            duration = float(row.get("duration_seconds") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        rows.append(
            {
                "name": row.get("name"),
                "path": row.get("path"),
                "status_code": row.get("status_code"),
                "duration_seconds": duration,
            }
        )
    return rows


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R26 Cloud UI Access Control Gate")
    decision = payload["access_control_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- SSH tunnel allowed now: `{decision['ssh_tunnel_allowed_now']}`",
            f"- Public HTTPS allowed now: `{decision['public_https_allowed_now']}`",
            f"- Public HTTPS review ready: `{decision['public_https_review_ready']}`",
            f"- Install/firewall allowed now: `{decision['install_or_firewall_allowed_now']}`",
            f"- Slow route count: `{decision['slow_route_count']}`",
            f"- Slowest route seconds: `{decision['slowest_route_seconds']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- No nginx, firewall, DNS, certificate, service, or SSH command was executed.",
            "- No public UI exposure was created.",
            "- No R5 process was started or stopped.",
            "- No paper/live/demo trades were created.",
            "",
            f"- Next Codex step: {decision['next_codex_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R26 Access Control Decision Detail")
    decision = payload["access_control_decision"]
    inputs = payload["input_parameters"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            f"- Domain: `{inputs.get('public_domain') or 'missing'}`",
            f"- Operator IP/CIDR: `{inputs.get('operator_ip_cidr') or 'missing'}`",
            f"- Auth mode: `{inputs.get('auth_mode')}`",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["access_control_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Exposure Options", ""])
    for row in payload["exposure_options"]:
        lines.append(f"- `{row['option']}`: `{row['status']}` - {row['notes']}")
    if payload["slow_routes"]:
        lines.extend(["", "## Slow Routes Blocking Public Review", ""])
        for row in payload["slow_routes"]:
            lines.append(f"- `{row['path']}` `{row['duration_seconds']}s`")
    return "\n".join(lines) + "\n"


def _render_https_draft(payload: dict[str, Any]) -> str:
    inputs = payload["input_parameters"]
    domain = inputs.get("public_domain") or "example.your-domain.com"
    cidr = inputs.get("operator_ip_cidr") or "YOUR_PUBLIC_IP/32"
    auth = inputs.get("auth_mode") or "none"
    lines = [
        "# Draft only. Do not install until a later operator-approved phase.",
        "# Public HTTPS is not allowed by Phase 3BB-R26.",
        "# Required before install: real domain, operator IP/CIDR allowlist, auth wall, TLS certificate, and R26/R27 approval.",
        "",
        f"# domain: {domain}",
        f"# operator_ip_cidr: {cidr}",
        f"# auth_mode: {auth}",
        "",
        "server {",
        "    listen 443 ssl http2;",
        f"    server_name {domain};",
        "",
        "    # ssl_certificate /etc/letsencrypt/live/DOMAIN/fullchain.pem;",
        "    # ssl_certificate_key /etc/letsencrypt/live/DOMAIN/privkey.pem;",
        "",
        f"    allow {cidr};",
        "    deny all;",
        "",
    ]
    if auth == "basic_auth":
        lines.extend(
            [
                "    auth_basic \"Kalshi operator UI\";",
                "    auth_basic_user_file /etc/nginx/kalshi-ui.htpasswd;",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "    # Add the selected auth layer here before install.",
                "    # Public exposure without auth is rejected.",
                "",
            ]
        )
    lines.extend(
        [
            "    location / {",
            "        proxy_pass http://127.0.0.1:8080;",
            "        proxy_http_version 1.1;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto https;",
            "    }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["access_control_decision"]["operator_next_command"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", "", f"printf '%s\\n' {_shell_quote(command)}", ""])


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R26 Next Actions")
    decision = payload["access_control_decision"]
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
            "- Do not expose the UI publicly yet.",
            "- Do not install nginx or open firewall ports yet.",
            "- Do not create paper trades.",
            "- Do not submit/cancel/replace live or demo orders.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _mark_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        return
