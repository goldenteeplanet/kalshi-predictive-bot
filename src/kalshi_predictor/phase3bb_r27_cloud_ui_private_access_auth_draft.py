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

PHASE3BB_R27_VERSION = "phase3bb_r27_cloud_ui_private_access_auth_draft_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r27")
SUPPORTED_PRIVATE_ACCESS_MODES = {
    "ssh_tunnel",
    "private_vpn",
    "cloudflare_access_tunnel",
}


@dataclass(frozen=True)
class Phase3BBR27CloudUiPrivateAccessAuthDraftArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    options_csv_path: Path
    selected_plan_path: Path
    checklist_path: Path
    no_install_draft_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r27_cloud_ui_private_access_auth_draft_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    preferred_access: str = "private_vpn",
    operator_email: str | None = None,
    operator_device_label: str | None = None,
) -> Phase3BBR27CloudUiPrivateAccessAuthDraftArtifacts:
    payload = build_phase3bb_r27_cloud_ui_private_access_auth_draft(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        preferred_access=preferred_access,
        operator_email=operator_email,
        operator_device_label=operator_device_label,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_private_access_auth_draft.md"
    json_path = output_dir / "cloud_ui_private_access_auth_draft.json"
    options_csv_path = output_dir / "private_access_options.csv"
    selected_plan_path = output_dir / "SELECTED_PRIVATE_ACCESS_PLAN.md"
    checklist_path = output_dir / "operator_private_access_review_checklist.md"
    no_install_draft_path = output_dir / "private_access_no_install.draft.sh"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _write_csv(
        options_csv_path,
        payload["private_access_options"],
        [
            "option",
            "status",
            "risk",
            "auth_boundary",
            "public_exposure",
            "operator_work",
            "notes",
        ],
    )
    selected_plan_path.write_text(_render_selected_plan(payload), encoding="utf-8")
    checklist_path.write_text(_render_checklist(payload), encoding="utf-8")
    no_install_draft_path.write_text(_render_no_install_draft(payload), encoding="utf-8")
    _mark_executable(no_install_draft_path)
    operator_command_path.write_text(_render_operator_command(payload), encoding="utf-8")
    _mark_executable(operator_command_path)
    next_actions_path.write_text(_render_next_actions(payload), encoding="utf-8")
    _write_manifest(
        manifest_path,
        [
            executive_summary_path,
            markdown_path,
            json_path,
            options_csv_path,
            selected_plan_path,
            checklist_path,
            no_install_draft_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR27CloudUiPrivateAccessAuthDraftArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        options_csv_path=options_csv_path,
        selected_plan_path=selected_plan_path,
        checklist_path=checklist_path,
        no_install_draft_path=no_install_draft_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r27_cloud_ui_private_access_auth_draft(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    preferred_access: str = "private_vpn",
    operator_email: str | None = None,
    operator_device_label: str | None = None,
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
        "command": "kalshi-bot phase3bb-r27-cloud-ui-private-access-auth-draft",
        "argv": command_args or [],
    }
    r26_path = reports_dir / "phase3bb_r26" / "cloud_ui_access_control_decision.json"
    r25_path = reports_dir / "phase3bb_r25" / "cloud_ui_operator_smoke_test.json"
    r24_path = reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json"
    r26 = _read_json(r26_path)
    r25 = _read_json(r25_path)
    r24 = _read_json(r24_path)
    inputs = {
        "preferred_access": _normalize_preferred_access(preferred_access),
        "operator_email": (operator_email or "").strip(),
        "operator_device_label": (operator_device_label or "").strip(),
    }
    checks = _draft_checks(r26, r25, r24, inputs)
    options = _private_access_options(r26, r25, inputs)
    decision = _decision(checks, options, r26, r25, r24, inputs)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "draft_only": True,
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
        "phase": "3BB-R27-CLOUD-UI-PRIVATE-ACCESS-AUTH-DRAFT",
        "phase_version": PHASE3BB_R27_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_PRIVATE_ACCESS_AUTH_DRAFT",
        "reports_dir": str(reports_dir),
        "r26_artifact_path": str(r26_path),
        "r25_artifact_path": str(r25_path),
        "r24_artifact_path": str(r24_path),
        "input_parameters": inputs,
        "draft_checks": checks,
        "private_access_options": options,
        "selected_private_access_plan": decision["selected_private_access_plan"],
        "private_access_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _draft_checks(
    r26: dict[str, Any],
    r25: dict[str, Any],
    r24: dict[str, Any],
    inputs: dict[str, Any],
) -> list[dict[str, Any]]:
    r26_decision = r26.get("access_control_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    preferred_access = inputs.get("preferred_access")
    return [
        _check("r26_artifact_present", bool(r26), "R26 access-control artifact exists."),
        _check(
            "r26_ssh_tunnel_allowed",
            r26_decision.get("ssh_tunnel_allowed_now") is True,
            f"R26 ssh_tunnel_allowed_now={r26_decision.get('ssh_tunnel_allowed_now')}.",
        ),
        _check(
            "r26_public_https_not_allowed",
            r26_decision.get("public_https_allowed_now") is False,
            f"R26 public_https_allowed_now={r26_decision.get('public_https_allowed_now')}.",
        ),
        _check(
            "r25_smoke_passed",
            r25_decision.get("status") == "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS",
            f"R25 status is {r25_decision.get('status')}.",
        ),
        _check(
            "r24_tunnel_ready",
            r24_decision.get("status") == "VERIFIED_UI_RUNNING_SSH_TUNNEL_READY",
            f"R24 status is {r24_decision.get('status')}.",
        ),
        _check(
            "preferred_access_supported",
            preferred_access in SUPPORTED_PRIVATE_ACCESS_MODES,
            f"preferred_access={preferred_access}; supported={', '.join(sorted(SUPPORTED_PRIVATE_ACCESS_MODES))}.",
        ),
    ]


def _private_access_options(
    r26: dict[str, Any],
    r25: dict[str, Any],
    inputs: dict[str, Any],
) -> list[dict[str, str]]:
    r26_decision = r26.get("access_control_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    slow_count = int(r26_decision.get("slow_route_count") or 0)
    smoke_ok = r25_decision.get("status") == "VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS"
    return [
        {
            "option": "SSH_TUNNEL_ONLY",
            "status": "APPROVED_NOW" if smoke_ok else "BLOCKED",
            "risk": "LOW",
            "auth_boundary": "SSH key and droplet user access",
            "public_exposure": "NONE",
            "operator_work": "Keep an SSH tunnel open from your workstation.",
            "notes": "Already working on 127.0.0.1:8081; best immediate path.",
        },
        {
            "option": "PRIVATE_VPN_OR_TAILSCALE",
            "status": "RECOMMENDED_DRAFT" if smoke_ok else "BLOCKED",
            "risk": "LOW_TO_MEDIUM",
            "auth_boundary": "Private network membership plus device identity",
            "public_exposure": "NONE",
            "operator_work": "Review a no-install draft, then approve a separate install phase if wanted.",
            "notes": "Best always-on ergonomic path while avoiding public web exposure.",
        },
        {
            "option": "CLOUDFLARE_ACCESS_TUNNEL",
            "status": "REVIEW_OPTION",
            "risk": "MEDIUM",
            "auth_boundary": "Identity-aware proxy policy",
            "public_exposure": "PROXIED",
            "operator_work": "Requires account/domain/policy review before any install.",
            "notes": "Could avoid inbound firewall exposure, but still adds external auth/provider dependency.",
        },
        {
            "option": "PUBLIC_HTTPS_BASIC_AUTH",
            "status": "DEFERRED_BLOCKED",
            "risk": "MEDIUM_TO_HIGH",
            "auth_boundary": "Nginx TLS plus IP allowlist plus basic auth",
            "public_exposure": "PUBLIC_443",
            "operator_work": "Requires domain, cert, IP allowlist, auth secret handling, and route performance fixes.",
            "notes": f"R26 public HTTPS is blocked; slow_route_count={slow_count}.",
        },
        {
            "option": "OPEN_PUBLIC_NO_AUTH",
            "status": "REJECTED",
            "risk": "HIGH",
            "auth_boundary": "NONE",
            "public_exposure": "PUBLIC",
            "operator_work": "Do not run.",
            "notes": "Rejected because the UI exposes operational state and has no built-in login wall.",
        },
    ]


def _decision(
    checks: list[dict[str, Any]],
    options: list[dict[str, str]],
    r26: dict[str, Any],
    r25: dict[str, Any],
    r24: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r26_decision = r26.get("access_control_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    selected = _select_plan(options, inputs.get("preferred_access") or "private_vpn")
    if failed:
        status = "BLOCKED_PRIVATE_ACCESS_DRAFT"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R26 - Restore Cloud UI Access Control Gate"
        operator_command = (
            "kalshi-bot phase3bb-r26-cloud-ui-access-control-gate "
            "--output-dir reports/phase3bb_r26 --reports-dir reports"
        )
    else:
        status = "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL"
        reason = (
            f"Drafted private access plan `{selected['option']}`. "
            "SSH tunnel remains approved now; public HTTPS remains blocked."
        )
        next_step = "Phase 3BB-R28 - Cloud UI Private Access Operator Review / No-Install Dry Run"
        operator_command = "Open http://127.0.0.1:8081 through the existing SSH tunnel."
    return {
        "status": status,
        "draft_ready": not failed,
        "selected_private_access_plan": selected,
        "ssh_tunnel_allowed_now": r26_decision.get("ssh_tunnel_allowed_now") is True and not failed,
        "public_https_allowed_now": False,
        "install_allowed_now": False,
        "firewall_change_allowed_now": False,
        "selected_option": selected["option"],
        "selected_option_status": selected["status"],
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r26_status": r26_decision.get("status"),
        "r25_status": r25_decision.get("status"),
        "r24_status": r24_decision.get("status"),
        "r5_pid": r26_decision.get("r5_pid") or r25_decision.get("r5_pid") or r24_decision.get("r5_pid"),
        "operator_next_command": operator_command,
        "next_codex_step": next_step,
    }


def _select_plan(options: list[dict[str, str]], preferred_access: str) -> dict[str, str]:
    target = {
        "ssh_tunnel": "SSH_TUNNEL_ONLY",
        "private_vpn": "PRIVATE_VPN_OR_TAILSCALE",
        "cloudflare_access_tunnel": "CLOUDFLARE_ACCESS_TUNNEL",
    }.get(preferred_access, "PRIVATE_VPN_OR_TAILSCALE")
    for option in options:
        if option["option"] == target:
            return option
    return options[0]


def _normalize_preferred_access(value: str) -> str:
    normalized = (value or "private_vpn").strip().lower().replace("-", "_")
    aliases = {
        "tailscale": "private_vpn",
        "vpn": "private_vpn",
        "private": "private_vpn",
        "tunnel": "ssh_tunnel",
        "ssh": "ssh_tunnel",
        "cloudflare": "cloudflare_access_tunnel",
        "cf_access": "cloudflare_access_tunnel",
    }
    return aliases.get(normalized, normalized)


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R27 Cloud UI Private Access/Auth Draft")
    decision = payload["private_access_decision"]
    selected = decision["selected_private_access_plan"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Draft ready: `{decision['draft_ready']}`",
            f"- Selected option: `{selected['option']}`",
            f"- Selected option status: `{selected['status']}`",
            f"- SSH tunnel allowed now: `{decision['ssh_tunnel_allowed_now']}`",
            f"- Public HTTPS allowed now: `{decision['public_https_allowed_now']}`",
            f"- Install allowed now: `{decision['install_allowed_now']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- Draft only; no private access software was installed.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R27 Private Access/Auth Draft Detail")
    decision = payload["private_access_decision"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["draft_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    lines.extend(["", "## Private Access Options", ""])
    for row in payload["private_access_options"]:
        lines.append(
            f"- `{row['option']}`: `{row['status']}` / `{row['risk']}` - {row['notes']}"
        )
    return "\n".join(lines) + "\n"


def _render_selected_plan(payload: dict[str, Any]) -> str:
    decision = payload["private_access_decision"]
    selected = decision["selected_private_access_plan"]
    lines = _metadata_lines(payload, "# Selected Private Access Plan")
    lines.extend(
        [
            "",
            f"- Selected option: `{selected['option']}`",
            f"- Status: `{selected['status']}`",
            f"- Risk: `{selected['risk']}`",
            f"- Auth boundary: {selected['auth_boundary']}",
            f"- Public exposure: `{selected['public_exposure']}`",
            f"- Operator work: {selected['operator_work']}",
            f"- Notes: {selected['notes']}",
            "",
            "## Current Approved Access",
            "",
            "- Keep using the SSH tunnel at `http://127.0.0.1:8081`.",
            "- Keep the droplet UI bound to `127.0.0.1:8080`.",
            "- Do not open inbound public web ports in this phase.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_checklist(payload: dict[str, Any]) -> str:
    decision = payload["private_access_decision"]
    selected = decision["selected_private_access_plan"]
    lines = _metadata_lines(payload, "# Private Access Operator Review Checklist")
    lines.extend(
        [
            "",
            "## Before Any Install",
            "",
            "- [ ] R24 still reports `VERIFIED_UI_RUNNING_SSH_TUNNEL_READY`.",
            "- [ ] R25 still reports `VERIFIED_CLOUD_UI_OPERATOR_SMOKE_PASS`.",
            "- [ ] R26 still blocks public HTTPS unless explicit public gates are satisfied.",
            f"- [ ] Selected access option is `{selected['option']}`.",
            "- [ ] Operator understands whether a third-party identity provider is involved.",
            "- [ ] No generated draft contains real secrets.",
            "- [ ] Any install phase uses a separate operator approval token.",
            "",
            "## Never In This Phase",
            "",
            "- [ ] Do not run apt installs.",
            "- [ ] Do not modify firewall rules.",
            "- [ ] Do not install nginx configs.",
            "- [ ] Do not expose the UI publicly.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_no_install_draft(payload: dict[str, Any]) -> str:
    selected = payload["private_access_decision"]["selected_private_access_plan"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r27] no-install private access/auth draft'",
            f"echo '[phase3bb-r27] selected option: {selected['option']}'",
            "echo '[phase3bb-r27] no remote commands are executed by this draft'",
            "echo '[phase3bb-r27] no firewall/nginx/service changes are made'",
            "echo '[phase3bb-r27] current approved access remains: SSH tunnel to http://127.0.0.1:8081'",
            "",
            "# Reference only. Do not execute install commands from this draft.",
            "# Current tunnel pattern:",
            "echo \"ssh -o ExitOnForwardFailure=yes -i ~/.ssh/id_ed25519_do -L 8081:127.0.0.1:8080 kalshi@159.65.35.72\"",
            "",
        ]
    )


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["private_access_decision"]["operator_next_command"]
    return "\n".join(["#!/usr/bin/env bash", "set -euo pipefail", "", f"printf '%s\\n' {_shell_quote(command)}", ""])


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R27 Next Actions")
    decision = payload["private_access_decision"]
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
            "- Do not install private access software yet.",
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
