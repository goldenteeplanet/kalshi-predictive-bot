from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.config import Settings, get_settings
from kalshi_predictor.institutional_dashboard.service import build_dashboard_snapshot
from kalshi_predictor.utils.time import utc_now


def generate_institutional_dashboard_report(
    session: Session,
    *,
    output_path: str | Path = "reports/institutional_dashboard.md",
    settings: Settings | None = None,
) -> Path:
    resolved = settings or get_settings()
    snapshot = build_dashboard_snapshot(session, settings=resolved)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 3T Institutional Dashboard Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Snapshot: `{snapshot['snapshot_id']}`",
        f"- Snapshot generated at: `{snapshot['generated_at']}`",
        f"- Effective query as-of: `{snapshot['effective_as_of']}`",
        f"- Mode: `{snapshot['dashboard_mode']}`",
        f"- Freshness: `{snapshot['freshness_status']}`",
        f"- Completeness: `{snapshot['completeness_status']}`",
        f"- Consistency: `{snapshot['consistency_mode']}`",
        (
            "- Cross-panel skew: "
            f"`{snapshot['cross_panel_skew']['value_seconds'] or 'n/a'} "
            f"{snapshot['cross_panel_skew']['unit']}` "
            f"from `{snapshot['cross_panel_skew']['oldest_source_id'] or 'n/a'}` "
            f"to `{snapshot['cross_panel_skew']['newest_source_id'] or 'n/a'}` "
            f"(threshold `{snapshot['cross_panel_skew']['threshold_seconds']}s`)"
        ),
        f"- Reconciliation: `{snapshot['reconciliation']['status']}`",
        f"- Database fingerprint: `{snapshot['runtime_context']['database_fingerprint']}`",
        f"- Git commit: `{snapshot['runtime_context']['git_commit']}`",
        "- Trading writes: `not available from Phase 3T`",
        "",
        "## Repository Assessment",
        "",
        "- UI stack: FastAPI, Jinja templates, static CSS/JavaScript.",
        "- Dashboard source pattern: existing repository helpers and SQLAlchemy sessions.",
        "- Auth/RBAC: local dashboard viewer semantics; no browser exchange credentials.",
        "- Read model: direct bounded reads from authoritative local tables.",
        "",
        "## Panel Catalog",
        "",
        "| Panel | Type | Criticality | Sources |",
        "| --- | --- | --- | --- |",
    ]
    for panel in snapshot["panel_registry"]:
        lines.append(
            "| "
            f"{panel['title']} | {panel['panel_type']} | {panel['criticality']} | "
            f"{', '.join(panel['sources'])} |"
        )
    lines.extend(
        [
            "",
            "## Source Watermarks",
            "",
            "| Source | State | Enabled | Rows | Last Attempt | Last Success | "
            "Data Watermark | Freshness | Error |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for source in snapshot["source_watermarks"]:
        lines.append(
            "| "
            f"{source['source_id']} | "
            f"{source['requirement_state']} / {source['lifecycle_state']} | "
            f"{source['enabled']} | {source['row_count']} | "
            f"{source['last_attempt_at'] or 'n/a'} | {source['last_success_at'] or 'n/a'} | "
            f"{source['data_watermark'] or 'n/a'} | {source['freshness_status']} | "
            f"{source['error'] or source['warning'] or ''} |"
        )
    lines.extend(
        [
            "",
            "## Producer Chain",
            "",
            "| Stage | Source | Status | Next Command |",
            "| --- | --- | --- | --- |",
        ]
    )
    for source in snapshot["producer_chain"]:
        lines.append(
            "| "
            f"{source['stage']} | {source['source_id']} | "
            f"{source['freshness_status']} / {source['completeness_status']} | "
            f"`{source['next_command'] or 'n/a'}` |"
        )
    lines.extend(
        [
            "",
            "## Reconciliation",
            "",
            "| Check | Status | Panel | Authoritative | Source |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for check in snapshot["reconciliation"]["checks"]:
        lines.append(
            "| "
            f"{check['check_id']} | {check['status']} | {check['panel_value']} | "
            f"{check['authoritative_value']} | {check['source']} |"
        )
    lines.extend(
        [
            "",
            "## Read-Only Proof",
            "",
            "- Phase 3T routes are read/query/export routes only.",
            "- No Phase 3T route creates, submits, modifies, or cancels orders.",
            "- Phase 3T displays Phase 3S recommendations as policy diagnostics, not orders.",
            "- Synthetic-market panels are labeled `SYNTHETIC INTERNAL NON_TRADABLE`.",
            "",
            "## Known Gaps",
            "",
            "- Full RBAC/SSO is not implemented in this local dashboard stack.",
            "- Live streaming is exposed as a heartbeat/snapshot stream, not a market delta feed.",
            "- Browser visual tests are not part of this local pytest suite.",
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def institutional_dashboard_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "mode": snapshot["dashboard_mode"],
        "freshness_status": snapshot["freshness_status"],
        "completeness_status": snapshot["completeness_status"],
        "panel_count": len(snapshot["panel_registry"]),
        "warning_count": len(snapshot["warnings"]),
        "reconciliation_status": snapshot["reconciliation"]["status"],
        "read_only": snapshot["read_only_boundary"]["read_only"],
    }
