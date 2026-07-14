from __future__ import annotations

from collections import Counter

from kalshi_predictor.synthetic_markets.contracts import DISCLAIMER, SyntheticMarketsResult
from kalshi_predictor.utils.decimals import decimal_to_str


def render_synthetic_markets_markdown(result: SyntheticMarketsResult) -> str:
    lines = [
        "# Phase 3R Synthetic Markets Report",
        "",
        DISCLAIMER,
        "",
        (
            "Synthetic market forecasts are internal research artifacts only. They are not "
            "Kalshi listings, order books, trade recommendations, demo orders, live orders, "
            "fills, or positions."
        ),
        "",
        "## Overall status",
        "",
        f"- Status: {result.status}",
        f"- Run ID: {result.run_id}",
        f"- Run type: {result.run_type}",
        f"- Mode: {result.mode}",
        f"- Started: {result.started_at.isoformat()}",
        f"- Completed: {result.completed_at.isoformat()}",
        f"- Candidates generated: {result.candidate_counts['generated']}",
        f"- Accepted estimates: {result.candidate_counts['accepted']}",
        f"- Rejected or paused: {result.candidate_counts['rejected']}",
        "",
        "## Highest-confidence unlisted synthetic markets",
        "",
    ]
    cards = sorted(
        result.cards,
        key=lambda card: (card.reliability.get("grade", "Z"), card.coherent_probability),
        reverse=True,
    )
    if not cards:
        lines.append("- None.")
    for card in cards:
        lines.extend(
            [
                f"- {card.synthetic_event.canonical_title}",
                f"  - Probability: {decimal_to_str(card.coherent_probability)}",
                f"  - Reliability: {card.reliability.get('grade', 'UNRATED')}",
                f"  - Listing status: {card.listing_check.status}",
                f"  - Valid until: {card.valid_until.isoformat()}",
                f"  - Card ID: {card.card_id}",
            ]
        )
    lines.extend(
        [
            "",
            "## New or materially changed estimates",
            "",
        ]
    )
    if not cards:
        lines.append("- None.")
    for card in cards:
        lines.extend(
            [
                f"- {card.estimate_id}",
                f"  - Raw probability: {decimal_to_str(card.raw_probability)}",
                f"  - Coherent probability: {decimal_to_str(card.coherent_probability)}",
                f"  - Interval: {card.interval.get('lower')} to {card.interval.get('upper')}",
            ]
        )
    lines.extend(["", "## Markets rejected or paused", ""])
    if not result.rejected_candidates:
        lines.append("- None.")
    for rejection in result.rejected_candidates:
        reasons = ", ".join(rejection.get("reason_codes", [])) or "unspecified"
        lines.append(f"- {rejection.get('candidate_id')}: {rejection.get('status')} ({reasons})")
    lines.extend(["", "## Coherence and data-quality warnings", ""])
    warnings = _warnings(result)
    if not warnings:
        lines.append("- None.")
    for warning in warnings:
        lines.append(f"- {warning}")
    lines.extend(["", "## New Kalshi listing matches", ""])
    matches = [
        match
        for card in result.cards
        for match in card.listing_check.matches
    ]
    if not matches:
        lines.append("- None.")
    for match in matches:
        lines.append(
            f"- {match.match_class}: {match.kalshi_market_ticker or 'n/a'} "
            f"(score {decimal_to_str(match.semantic_score)})"
        )
    lines.extend(
        [
            "",
            "## Resolved markets and calibration results",
            "",
            "- No Phase 3R synthetic markets resolved in this run.",
            "",
            "## Rejection breakdown",
            "",
        ]
    )
    rejection_counts = Counter(
        reason
        for rejection in result.rejected_candidates
        for reason in rejection.get("reason_codes", [])
    )
    if not rejection_counts:
        lines.append("- None.")
    for reason, count in sorted(rejection_counts.items()):
        lines.append(f"- {reason}: {count}")
    lines.extend(
        [
            "",
            "## Phase 3O / 3P / 3Q evidence",
            "",
            "- Phase 3O: accepted estimates write market_memory and forecast_memory only.",
            "- Phase 3P: report output can be referenced by self-evaluation journals.",
            (
                "- Phase 3Q: cards include deterministic lineage and feature placeholders "
                "for research export."
            ),
            "",
            "## Recommended next action",
            "",
            _recommended_next_action(result),
            "",
        ]
    )
    return "\n".join(lines)


def _warnings(result: SyntheticMarketsResult) -> list[str]:
    warnings: list[str] = []
    for check in result.listing_checks:
        warnings.extend(check.warnings)
        if check.status == "LISTING_STATUS_UNKNOWN":
            warnings.append(
                f"{check.listing_check_id}: listing status unknown; estimate paused or suppressed."
            )
    for card in result.cards:
        warnings.extend(card.constraint_result.get("warnings", []))
        warnings.extend(f"{card.estimate_id}: missing {item}" for item in card.missing_inputs)
    return sorted(set(warnings))


def _recommended_next_action(result: SyntheticMarketsResult) -> str:
    if result.status == "DISABLED":
        return "- Enable with PHASE_3R_SYNTHETIC_MARKETS_ENABLED=true and PHASE_3R_MODE=shadow."
    if not result.cards and result.rejected_candidates:
        return (
            "- Review rejected candidate reason codes and provide resolvable public-source inputs."
        )
    if not result.cards:
        return "- Add approved candidate JSON with objective settlement rules."
    return "- Monitor listing matches and later resolution outcomes; do not trade synthetic cards."
