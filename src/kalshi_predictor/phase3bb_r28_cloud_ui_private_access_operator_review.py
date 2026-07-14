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

PHASE3BB_R28_VERSION = "phase3bb_r28_cloud_ui_private_access_operator_review_v1"
DEFAULT_OUTPUT_DIR = Path("reports/phase3bb_r28")
READY_R27_STATUS = "PRIVATE_ACCESS_DRAFT_READY_NO_INSTALL"
SAFE_PRIVATE_OPTIONS = {"SSH_TUNNEL_ONLY", "PRIVATE_VPN_OR_TAILSCALE", "CLOUDFLARE_ACCESS_TUNNEL"}


@dataclass(frozen=True)
class Phase3BBR28CloudUiPrivateAccessOperatorReviewArtifacts:
    output_dir: Path
    executive_summary_path: Path
    markdown_path: Path
    json_path: Path
    checks_csv_path: Path
    dry_run_path: Path
    handoff_preview_path: Path
    operator_command_path: Path
    next_actions_path: Path
    manifest_path: Path


def write_phase3bb_r28_cloud_ui_private_access_operator_review_report(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    selected_access: str | None = None,
) -> Phase3BBR28CloudUiPrivateAccessOperatorReviewArtifacts:
    payload = build_phase3bb_r28_cloud_ui_private_access_operator_review(
        session,
        output_dir=output_dir,
        reports_dir=reports_dir,
        settings=settings,
        command_args=command_args,
        selected_access=selected_access,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    executive_summary_path = output_dir / "EXECUTIVE_SUMMARY.md"
    markdown_path = output_dir / "cloud_ui_private_access_operator_review.md"
    json_path = output_dir / "cloud_ui_private_access_operator_review.json"
    checks_csv_path = output_dir / "operator_review_checks.csv"
    dry_run_path = output_dir / "operator_private_access_no_install_dry_run.sh"
    handoff_preview_path = output_dir / "OPERATOR_APPROVED_INSTALL_HANDOFF_PREVIEW.md"
    operator_command_path = output_dir / "operator_next_command.sh"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    manifest_path = output_dir / "MANIFEST.sha256"

    executive_summary_path.write_text(_render_executive_summary(payload), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    _write_csv(checks_csv_path, payload["operator_review_checks"], ["check", "passed", "detail"])
    dry_run_path.write_text(_render_dry_run(payload), encoding="utf-8")
    _mark_executable(dry_run_path)
    handoff_preview_path.write_text(_render_handoff_preview(payload), encoding="utf-8")
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
            dry_run_path,
            handoff_preview_path,
            operator_command_path,
            next_actions_path,
        ],
    )
    return Phase3BBR28CloudUiPrivateAccessOperatorReviewArtifacts(
        output_dir=output_dir,
        executive_summary_path=executive_summary_path,
        markdown_path=markdown_path,
        json_path=json_path,
        checks_csv_path=checks_csv_path,
        dry_run_path=dry_run_path,
        handoff_preview_path=handoff_preview_path,
        operator_command_path=operator_command_path,
        next_actions_path=next_actions_path,
        manifest_path=manifest_path,
    )


def build_phase3bb_r28_cloud_ui_private_access_operator_review(
    session: Session,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    settings: Settings | None = None,
    command_args: list[str] | None = None,
    selected_access: str | None = None,
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
        "command": "kalshi-bot phase3bb-r28-cloud-ui-private-access-operator-review",
        "argv": command_args or [],
    }
    r27_path = reports_dir / "phase3bb_r27" / "cloud_ui_private_access_auth_draft.json"
    r26_path = reports_dir / "phase3bb_r26" / "cloud_ui_access_control_decision.json"
    r25_path = reports_dir / "phase3bb_r25" / "cloud_ui_operator_smoke_test.json"
    r24_path = reports_dir / "phase3bb_r24" / "cloud_ui_start_tunnel_verification.json"
    r27 = _read_json(r27_path)
    r26 = _read_json(r26_path)
    r25 = _read_json(r25_path)
    r24 = _read_json(r24_path)
    requested_access = _normalize_selected_access(selected_access)
    selected_plan = _selected_plan(r27, requested_access)
    checks = _review_checks(r27, r26, r25, r24, selected_plan)
    decision = _decision(checks, selected_plan, requested_access, r27, r26, r25, r24)
    safety = {
        **_safety_flags(),
        "paper_only": True,
        "diagnostic_only": True,
        "no_install_dry_run": True,
        "no_deploy": True,
        "no_service_install": True,
        "no_service_start": True,
        "no_service_enable": True,
        "no_private_access_install": True,
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
        "phase": "3BB-R28-CLOUD-UI-PRIVATE-ACCESS-OPERATOR-REVIEW",
        "phase_version": PHASE3BB_R28_VERSION,
        "mode": "PAPER_READ_ONLY_CLOUD_UI_PRIVATE_ACCESS_OPERATOR_REVIEW_NO_INSTALL",
        "reports_dir": str(reports_dir),
        "r27_artifact_path": str(r27_path),
        "r26_artifact_path": str(r26_path),
        "r25_artifact_path": str(r25_path),
        "r24_artifact_path": str(r24_path),
        "requested_selected_access": requested_access,
        "selected_private_access_plan": selected_plan,
        "operator_review_checks": checks,
        "operator_review_decision": decision,
        "next_operator_command": decision["operator_next_command"],
        "safety_flags": safety,
        "live_or_demo_execution": False,
        "order_submission": False,
        "order_cancel_replace": False,
        "paper_trade_creation": False,
        "thresholds_lowered": False,
    }


def _review_checks(
    r27: dict[str, Any],
    r26: dict[str, Any],
    r25: dict[str, Any],
    r24: dict[str, Any],
    selected_plan: dict[str, Any],
) -> list[dict[str, Any]]:
    r27_decision = r27.get("private_access_decision") or {}
    r26_decision = r26.get("access_control_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    option = str(selected_plan.get("option") or "")
    return [
        _check("r27_artifact_present", bool(r27), "R27 private access draft artifact exists."),
        _check(
            "r27_draft_ready",
            r27_decision.get("status") == READY_R27_STATUS,
            f"R27 status is {r27_decision.get('status')}.",
        ),
        _check(
            "r26_public_https_blocked",
            r26_decision.get("public_https_allowed_now") is False,
            f"R26 public_https_allowed_now={r26_decision.get('public_https_allowed_now')}.",
        ),
        _check(
            "r26_ssh_tunnel_allowed",
            r26_decision.get("ssh_tunnel_allowed_now") is True,
            f"R26 ssh_tunnel_allowed_now={r26_decision.get('ssh_tunnel_allowed_now')}.",
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
            "selected_option_safe",
            option in SAFE_PRIVATE_OPTIONS,
            f"Selected option is {option}.",
        ),
        _check(
            "selected_option_not_public_open",
            selected_plan.get("public_exposure") in {"NONE", "PROXIED"},
            f"Selected public_exposure={selected_plan.get('public_exposure')}.",
        ),
        _check(
            "selected_option_not_rejected",
            selected_plan.get("status") not in {"REJECTED", "DEFERRED_BLOCKED"},
            f"Selected status={selected_plan.get('status')}.",
        ),
    ]


def _decision(
    checks: list[dict[str, Any]],
    selected_plan: dict[str, Any],
    requested_access: str | None,
    r27: dict[str, Any],
    r26: dict[str, Any],
    r25: dict[str, Any],
    r24: dict[str, Any],
) -> dict[str, Any]:
    failed = [row for row in checks if not row["passed"]]
    r27_decision = r27.get("private_access_decision") or {}
    r26_decision = r26.get("access_control_decision") or {}
    r25_decision = r25.get("smoke_decision") or {}
    r24_decision = r24.get("verification_decision") or {}
    if failed:
        status = "BLOCKED_PRIVATE_ACCESS_OPERATOR_REVIEW"
        reason = f"First failing check: {failed[0]['check']}."
        next_step = "Phase 3BB-R27 - Refresh Cloud UI Private Access/Auth Draft"
        operator_command = (
            "kalshi-bot phase3bb-r27-cloud-ui-private-access-auth-draft "
            "--output-dir reports/phase3bb_r27 --reports-dir reports --preferred-access private_vpn"
        )
    else:
        status = "PRIVATE_ACCESS_OPERATOR_REVIEW_READY_NO_INSTALL"
        reason = (
            f"Operator review dry run is ready for `{selected_plan.get('option')}`. "
            "No install, firewall, nginx, or public exposure is approved in R28."
        )
        next_step = "Phase 3BB-R29 - Operator-Approved Cloud UI Private Access Install Handoff"
        operator_command = (
            "Review reports/phase3bb_r28/"
            "OPERATOR_APPROVED_INSTALL_HANDOFF_PREVIEW.md"
        )
    return {
        "status": status,
        "review_ready": not failed,
        "selected_option": selected_plan.get("option"),
        "selected_option_status": selected_plan.get("status"),
        "requested_selected_access": requested_access,
        "install_allowed_now": False,
        "private_access_install_allowed_now": False,
        "public_https_allowed_now": False,
        "firewall_change_allowed_now": False,
        "ssh_tunnel_allowed_now": r26_decision.get("ssh_tunnel_allowed_now") is True and not failed,
        "requires_explicit_operator_approval_for_install": True,
        "failed_check_count": len(failed),
        "first_failed_check": failed[0]["check"] if failed else None,
        "primary_reason": reason,
        "r27_status": r27_decision.get("status"),
        "r26_status": r26_decision.get("status"),
        "r25_status": r25_decision.get("status"),
        "r24_status": r24_decision.get("status"),
        "r5_pid": (
            r27_decision.get("r5_pid")
            or r26_decision.get("r5_pid")
            or r25_decision.get("r5_pid")
            or r24_decision.get("r5_pid")
        ),
        "operator_next_command": operator_command,
        "next_codex_step": next_step,
    }


def _selected_plan(r27: dict[str, Any], requested_access: str | None) -> dict[str, Any]:
    options = r27.get("private_access_options") or []
    decision = r27.get("private_access_decision") or {}
    selected = (
        decision.get("selected_private_access_plan")
        or r27.get("selected_private_access_plan")
        or {}
    )
    requested_option = _requested_option_name(requested_access)
    if requested_option:
        for option in options:
            if option.get("option") == requested_option:
                return dict(option)
    return dict(selected)


def _requested_option_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower().replace("-", "_")
    return {
        "ssh": "SSH_TUNNEL_ONLY",
        "ssh_tunnel": "SSH_TUNNEL_ONLY",
        "private": "PRIVATE_VPN_OR_TAILSCALE",
        "private_vpn": "PRIVATE_VPN_OR_TAILSCALE",
        "vpn": "PRIVATE_VPN_OR_TAILSCALE",
        "tailscale": "PRIVATE_VPN_OR_TAILSCALE",
        "cloudflare": "CLOUDFLARE_ACCESS_TUNNEL",
        "cloudflare_access": "CLOUDFLARE_ACCESS_TUNNEL",
        "cloudflare_access_tunnel": "CLOUDFLARE_ACCESS_TUNNEL",
    }.get(normalized)


def _normalize_selected_access(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower().replace("-", "_") or None


def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "passed": bool(passed), "detail": detail}


def _render_executive_summary(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R28 Cloud UI Private Access Operator Review")
    decision = payload["operator_review_decision"]
    lines.extend(
        [
            "",
            "## Result",
            "",
            f"- Status: `{decision['status']}`",
            f"- Review ready: `{decision['review_ready']}`",
            f"- Selected option: `{decision['selected_option']}`",
            f"- Selected option status: `{decision['selected_option_status']}`",
            f"- Install allowed now: `{decision['install_allowed_now']}`",
            "- Private access install allowed now: "
            f"`{decision['private_access_install_allowed_now']}`",
            f"- Public HTTPS allowed now: `{decision['public_https_allowed_now']}`",
            f"- First failed check: `{decision['first_failed_check']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Safety",
            "",
            "- No private access software was installed.",
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
    lines = _metadata_lines(payload, "# Phase 3BB-R28 Operator Review Detail")
    decision = payload["operator_review_decision"]
    selected = payload["selected_private_access_plan"]
    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Status: `{decision['status']}`",
            f"- Reason: {decision['primary_reason']}",
            "",
            "## Selected Plan",
            "",
            f"- Option: `{selected.get('option')}`",
            f"- Status: `{selected.get('status')}`",
            f"- Risk: `{selected.get('risk')}`",
            f"- Auth boundary: {selected.get('auth_boundary')}",
            f"- Public exposure: `{selected.get('public_exposure')}`",
            f"- Operator work: {selected.get('operator_work')}",
            "",
            "## Checks",
            "",
        ]
    )
    for row in payload["operator_review_checks"]:
        marker = "PASS" if row["passed"] else "FAIL"
        lines.append(f"- `{marker}` `{row['check']}` - {row['detail']}")
    return "\n".join(lines) + "\n"


def _render_dry_run(payload: dict[str, Any]) -> str:
    decision = payload["operator_review_decision"]
    selected = payload["selected_private_access_plan"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            "echo '[phase3bb-r28] private access operator review dry run'",
            f"echo '[phase3bb-r28] selected option: {selected.get('option')}'",
            "test -f reports/phase3bb_r27/cloud_ui_private_access_auth_draft.json",
            "test -f reports/phase3bb_r26/cloud_ui_access_control_decision.json",
            "test -f reports/phase3bb_r25/cloud_ui_operator_smoke_test.json",
            "test -f reports/phase3bb_r24/cloud_ui_start_tunnel_verification.json",
            "echo '[phase3bb-r28] artifact checks passed'",
            "echo '[phase3bb-r28] no install commands are executed'",
            "echo '[phase3bb-r28] no remote commands are executed'",
            "echo '[phase3bb-r28] no firewall/nginx/service changes are made'",
            f"echo '[phase3bb-r28] decision: {decision['status']}'",
            "",
        ]
    )


def _render_handoff_preview(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Operator-Approved Install Handoff Preview")
    decision = payload["operator_review_decision"]
    selected = payload["selected_private_access_plan"]
    lines.extend(
        [
            "",
            "## Preview Only",
            "",
            "This is not an install script. It describes what a later R29 handoff would need.",
            "",
            f"- Selected option: `{selected.get('option')}`",
            f"- Review status: `{decision['status']}`",
            "- Required before R29: explicit operator approval token.",
            "- Required before R29: rerun R24/R25/R26/R27/R28 if cloud state changes.",
            "- Required before R29: confirm no public HTTPS exposure is being introduced.",
            "",
            "## R29 Must Still Forbid",
            "",
            "- Public open-internet UI without auth.",
            "- Live/demo exchange orders.",
            "- Paper trade creation.",
            "- R5 duplicate starts.",
        ]
    )
    if selected.get("option") == "PRIVATE_VPN_OR_TAILSCALE":
        lines.extend(
            [
                "",
                "## Private VPN/Tailscale Handoff Notes",
                "",
                "- Install should be operator-approved and separate.",
                "- Access should remain private network only.",
                "- The UI should continue binding to `127.0.0.1:8080` unless a later "
                "reviewed service change says otherwise.",
                "- Do not open ports 80/443 as part of the private access install.",
            ]
        )
    return "\n".join(lines) + "\n"


def _render_operator_command(payload: dict[str, Any]) -> str:
    command = payload["operator_review_decision"]["operator_next_command"]
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            f"printf '%s\\n' {_shell_quote(command)}",
            "",
        ]
    )


def _render_next_actions(payload: dict[str, Any]) -> str:
    lines = _metadata_lines(payload, "# Phase 3BB-R28 Next Actions")
    decision = payload["operator_review_decision"]
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
            "- Do not install private access software yet.",
            "- Do not expose the UI publicly.",
            "- Do not install nginx or open firewall ports.",
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
