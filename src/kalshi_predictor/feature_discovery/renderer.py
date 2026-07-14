from __future__ import annotations

from collections import Counter

from kalshi_predictor.feature_discovery.contracts import (
    STATUS_REJECTED,
    STATUS_VALIDATED,
    STATUS_WATCHLIST,
    FeatureDiscoveryResult,
)
from kalshi_predictor.utils.decimals import decimal_to_str


def render_feature_discovery_markdown(result: FeatureDiscoveryResult) -> str:
    counts = result.candidate_counts
    rejected_reasons = Counter(
        reason
        for evaluation in result.candidate_evaluations
        if evaluation.status == STATUS_REJECTED
        for reason in evaluation.reason_codes
    )
    lines = [
        "# Phase 3Q Feature Discovery Report",
        "",
        "## Run and data status",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Run type: `{result.run_type}`",
        f"- Status: `{result.status}`",
        f"- Training as of: `{result.training_as_of.isoformat()}`",
        f"- Data mode: `{result.manifest.data_mode}`",
        f"- Rows included: {result.manifest.rows_included} / {result.manifest.rows_total}",
        f"- Manifest hash: `{result.manifest.manifest_hash}`",
        "",
        "## Executive summary",
        "",
        f"- Candidates generated: {counts['generated']}",
        f"- Validated: {counts['validated']}",
        f"- Watchlist: {counts['watchlist']}",
        f"- Rejected: {counts['rejected']}",
        "- Production mutation: forbidden",
        "- Every non-`NO_ACTION` recommendation requires `HUMAN_REVIEW_REQUIRED`.",
        "",
        "## Features that robustly predicted profitable outcomes",
        "",
    ]
    _append_candidate_table(lines, result, STATUS_VALIDATED)
    lines.extend(["", "## Emerging candidates on the watchlist", ""])
    _append_candidate_table(lines, result, STATUS_WATCHLIST)
    lines.extend(["", "## Features that looked promising but failed validation", ""])
    _append_candidate_table(lines, result, STATUS_REJECTED)
    lines.extend(
        [
            "",
            "## Existing features whose value changed",
            "",
            "- Existing-feature decay monitoring is scaffolded through Phase 3Q scorecards; "
            "dedicated production-feature lineage remains `NOT_AVAILABLE` until a versioned "
            "production feature registry is added.",
            "",
            "## Segment-specific relationships",
            "",
        ]
    )
    for evaluation in result.candidate_evaluations[: result.manifest.rows_included or 5]:
        for segment in evaluation.segment_results:
            lines.append(
                "- "
                f"`{evaluation.candidate.feature_name}` / {segment['segment_key']}="
                f"{segment['segment_value']}: {segment['status']} "
                f"(n={segment['sample_size']})"
            )
    if not any(evaluation.segment_results for evaluation in result.candidate_evaluations):
        lines.append("- No segment evidence available.")
    lines.extend(
        [
            "",
            "## Redundancy and feature-family findings",
            "",
            "- Candidate IDs are canonicalized by expression and source lineage.",
            "- Correlated-family grouping is represented by `feature_family`; deeper graph "
            "analysis remains a deferred integration.",
            "",
            "## Recommended research experiments",
            "",
        ]
    )
    recommendations = [
        evaluation
        for evaluation in result.candidate_evaluations
        if evaluation.recommendation_action != "NO_ACTION"
    ]
    if recommendations:
        for evaluation in recommendations:
            lines.append(
                "- "
                f"`{evaluation.candidate.feature_name}` -> "
                f"{evaluation.recommendation_action} "
                "(HUMAN_REVIEW_REQUIRED)"
            )
    else:
        lines.append("- No experiment recommendation met the governed evidence gate.")
    lines.extend(
        [
            "",
            "## Data quality, selection bias, and unresolved labels",
            "",
        ]
    )
    for reason, count in sorted(result.manifest.excluded_counts.items()):
        lines.append(f"- Excluded `{reason}`: {count}")
    for source in result.manifest.unavailable_sources:
        lines.append(f"- Missing authoritative source: `{source}`")
    if rejected_reasons:
        lines.append("")
        lines.append("Rejected candidate reasons:")
        for reason, count in sorted(rejected_reasons.items()):
            lines.append(f"- `{reason}`: {count}")
    lines.extend(
        [
            "",
            "## Methodology and validation policy",
            "",
            "- Source rows are read from Phase 3O memory at a frozen `training_as_of` cutoff.",
            "- Features observed after the decision timestamp are excluded.",
            "- Labels finalized after `training_as_of` are excluded.",
            "- Baseline and candidate comparisons use identical rows.",
            "- Multiple testing uses deterministic Benjamini-Hochberg q-value adjustment.",
            "- Forecast-only rows may support watchlist evidence but cannot prove net P&L.",
            "",
            "## Evidence appendix",
            "",
        ]
    )
    for evaluation in result.candidate_evaluations:
        lines.extend(
            [
                f"### {evaluation.candidate.feature_name}",
                "",
                f"- Candidate ID: `{evaluation.candidate.candidate_id}`",
                f"- Family: `{evaluation.candidate.feature_family}`",
                f"- Status: `{evaluation.status}`",
                f"- Sample size: {evaluation.sample_size}",
                f"- Baseline rate: {_format_metric(evaluation.baseline_rate)}",
                f"- Candidate rate: {_format_metric(evaluation.candidate_rate)}",
                f"- Paired delta: {_format_metric(evaluation.paired_delta)}",
                f"- Economic effect: {_format_metric(evaluation.economic_effect)}",
                f"- Stability: {_format_metric(evaluation.stability_score)}",
                f"- Q-value: {_format_metric(evaluation.q_value)}",
                f"- Reason codes: {', '.join(evaluation.reason_codes) or 'none'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _append_candidate_table(lines: list[str], result: FeatureDiscoveryResult, status: str) -> None:
    matches = [
        evaluation
        for evaluation in result.candidate_evaluations
        if evaluation.status == status
    ][:25]
    if not matches:
        lines.append("- None.")
        return
    lines.append("| Feature | n | Delta | Economic | Stability | q | Reason |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for evaluation in matches:
        lines.append(
            "| "
            f"`{evaluation.candidate.feature_name}` | "
            f"{evaluation.sample_size} | "
            f"{_format_metric(evaluation.paired_delta)} | "
            f"{_format_metric(evaluation.economic_effect)} | "
            f"{_format_metric(evaluation.stability_score)} | "
            f"{_format_metric(evaluation.q_value)} | "
            f"{', '.join(evaluation.reason_codes) or 'none'} |"
        )


def _format_metric(value) -> str:
    return decimal_to_str(value) if value is not None else "NOT_AVAILABLE"
