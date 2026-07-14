from __future__ import annotations

from typing import Any


def render_journal_markdown(payload: dict[str, Any]) -> str:
    session = payload["trading_session"]
    coverage = payload["coverage_summary"]
    lines = [
        f"# Trading Self-Evaluation - {session['session_label']}",
        "",
        f"**Status:** {payload['journal_status']}",
        f"**Session:** {session['session_open_at']} to {session['session_close_at']} "
        f"({session['session_timezone']})",
        f"**Evaluation cutoff:** {payload['evaluation_as_of']}",
        f"**Data mode:** {payload['data_mode']}",
        (
            "**Coverage:** "
            f"{coverage['finalized_forecasts']}/{coverage['eligible_forecasts']} forecasts "
            f"finalized; {coverage['finalized_trades']}/{coverage['eligible_trades']} "
            "trades finalized"
        ),
        "",
        f"> {payload['headline']}",
        "",
        "## What worked",
        "",
    ]
    lines.extend(
        _finding_block(
            payload["what_worked"],
            "No supported what-worked finding met the configured evidence threshold.",
        )
    )
    lines.extend(["", "## What failed", ""])
    lines.extend(
        _finding_block(
            payload["what_failed"],
            "No supported failure met the configured evidence threshold.",
        )
    )
    lines.extend(["", "## What changed", ""])
    lines.extend(
        _finding_block(
            payload["what_changed"],
            "No deterministic or evidence-supported change was detected.",
        )
    )
    lines.extend(
        [
            "",
            "## Session and data status",
            "",
            f"- Trading session id: `{session['trading_session_id']}`",
            f"- Calendar id: `{session['calendar_id']}`",
            f"- Journal revision: {payload['journal_revision']}",
            f"- Reliability: {coverage['reliability_grade']}",
            f"- Quality flags: {', '.join(coverage['quality_flags']) or 'none'}",
            "",
            "## Executive summary",
            "",
            payload["executive_summary"],
            "",
            "## Risk and sizing review",
            "",
            payload["risk_and_sizing_summary"]["summary"],
            "",
            "## Forecast and opportunity review",
            "",
            payload["forecast_and_opportunity_summary"]["summary"],
            "",
            "## Trade and execution review",
            "",
            payload["trade_and_execution_summary"]["summary"],
            "",
            "## Model/version review",
            "",
            payload["model_and_version_summary"]["summary"],
            "",
            "## Data-quality and operational review",
            "",
        ]
    )
    if payload["data_quality_items"]:
        for finding in payload["data_quality_items"]:
            lines.append(
                f"- **{finding['severity']} - {finding['title']}:** "
                f"{finding['concise_statement']} "
                f"(reliability: {finding['reliability_grade']})"
            )
    else:
        lines.append("No material data-quality or operational issue was detected.")
    lines.extend(["", "## Open hypotheses", ""])
    hypotheses = [
        f"- `{finding['finding_id']}` {finding['hypothesis']}"
        for finding in payload["what_failed"]
        if finding.get("hypothesis")
    ]
    lines.extend(hypotheses or ["No open hypothesis is justified by the current evidence."])
    lines.extend(["", "## Recommended follow-ups", ""])
    if payload["recommended_follow_ups"]:
        for follow_up in payload["recommended_follow_ups"]:
            lines.extend(
                [
                    f"### {follow_up['title']} `{follow_up['follow_up_id']}`",
                    "",
                    f"- **Type:** {follow_up['type']}",
                    f"- **Priority:** {follow_up['priority']}",
                    f"- **Status:** {follow_up['status']}",
                    f"- **Rationale:** {follow_up['rationale']}",
                    f"- **Scope:** {follow_up['proposed_scope']}",
                    f"- **Success metric:** {follow_up['success_metric']}",
                    f"- **Minimum sample/duration:** {follow_up['minimum_sample_or_duration']}",
                    "",
                ]
            )
    else:
        lines.append("No action is justified by the current evidence.")
    lines.extend(
        [
            "## Unresolved outcomes and caveats",
            "",
            f"- Pending forecasts: {payload['unresolved_outcomes']['pending_forecasts']}",
            f"- Open trades: {payload['unresolved_outcomes']['open_trades']}",
            (
                "- Preliminary settlements: "
                f"{payload['unresolved_outcomes']['preliminary_settlements']}"
            ),
            (
                "- Late/missing partitions: "
                f"{payload['unresolved_outcomes']['late_or_missing_partitions']}"
            ),
            f"- Metrics that may change: {payload['unresolved_outcomes']['may_change_metrics']}",
            (
                "- Next revision condition: "
                f"{payload['unresolved_outcomes']['next_revision_condition']}"
            ),
        ]
    )
    for caveat in payload["caveats"]:
        lines.append(f"- {caveat}")
    lines.extend(
        [
            "",
            "## Watch items",
            "",
        ]
    )
    if payload["watch_items"]:
        for finding in payload["watch_items"]:
            lines.append(
                f"- **{finding['title']}:** {finding['concise_statement']} - "
                f"n={finding['sample_size']}, reliability={finding['reliability_grade']}"
            )
    else:
        lines.append("No watch item is currently open.")
    lines.extend(
        [
            "",
            "## Evidence appendix",
            "",
            f"- Evaluation run: `{payload['evaluation_run_id']}`",
            f"- Journal: `{payload['journal_id']}` revision {payload['journal_revision']}",
            (
                "- Dataset manifest: "
                f"`{payload['source_manifest_summary']['phase_3o_dataset_id']}`"
            ),
            f"- Input checksum: `{payload['source_manifest_summary']['input_checksum']}`",
            (
                "- Metric records: "
                f"{', '.join(payload['evidence_appendix']['metric_record_ids']) or 'none'}"
            ),
            (
                "- Finding records: "
                f"{', '.join(payload['evidence_appendix']['finding_ids']) or 'none'}"
            ),
            (
                "- Excluded rows: "
                f"{payload['evidence_appendix']['excluded_rows_by_reason']}"
            ),
            "",
            "### Key metrics",
            "",
            "| Metric | Cohort | Value | Sample | Reliability |",
            "|---|---|---:|---:|---|",
        ]
    )
    for metric in payload["key_metrics"]:
        lines.append(
            "| "
            f"{metric['metric_name']} | {metric['cohort']} | "
            f"{metric.get('value') if metric.get('value') is not None else ''} | "
            f"{metric['sample_size']} | {metric['reliability_grade']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _finding_block(findings: list[dict[str, Any]], empty: str) -> list[str]:
    if not findings:
        return [empty]
    lines: list[str] = []
    for finding in findings:
        lines.extend(
            [
                f"### {finding['title']} `{finding['finding_id']}`",
                "",
                finding["concise_statement"],
                "",
                f"- **Current:** {finding.get('current_value')}",
                (
                    "- **Baseline:** "
                    f"{finding.get('baseline', {}).get('value') or 'n/a'} "
                    f"({finding.get('baseline', {}).get('baseline_type') or 'NONE'}, "
                    f"n={finding.get('baseline', {}).get('sample_size') or 0})"
                ),
                f"- **Sample:** n={finding['sample_size']}",
                f"- **Effect:** {finding.get('effect_size') or 'n/a'}",
                f"- **Reliability:** {finding['reliability_grade']}",
                f"- **Attribution:** {finding['attribution_level']}",
                f"- **Why it matters:** {finding.get('detailed_explanation') or ''}",
                "",
            ]
        )
    return lines
