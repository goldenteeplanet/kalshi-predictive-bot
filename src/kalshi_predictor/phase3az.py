from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AZ_VERSION = "phase3az_v1"
PHASE_3AZ_R11_VERSION = "phase3az_r11_v1"


@dataclass(frozen=True)
class Phase3AZArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class Phase3AZR11ArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    next_actions_path: Path


def write_phase3az_gap_analysis_report(
    *,
    output_dir: Path = Path("reports/phase3az"),
    reports_dir: Path = Path("reports"),
) -> Phase3AZArtifactSet:
    payload = build_phase3az_gap_analysis(reports_dir=reports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3az_gap_analysis.json"
    markdown_path = output_dir / "phase3az_gap_analysis.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return Phase3AZArtifactSet(output_dir, json_path, markdown_path)


def write_phase3az_r11_non_crypto_activation_report(
    *,
    output_dir: Path = Path("reports/phase3az_r11"),
    reports_dir: Path = Path("reports"),
    weather_location_counts: list[dict[str, Any]] | None = None,
) -> Phase3AZR11ArtifactSet:
    payload = build_phase3az_r11_non_crypto_activation(
        reports_dir=reports_dir,
        weather_location_counts=weather_location_counts,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "non_crypto_category_activation.json"
    markdown_path = output_dir / "non_crypto_category_activation.md"
    next_actions_path = output_dir / "NEXT_ACTIONS.md"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    markdown = _render_r11_markdown(payload)
    markdown_path.write_text(markdown, encoding="utf-8")
    next_actions_path.write_text(_render_r11_next_actions(payload), encoding="utf-8")
    return Phase3AZR11ArtifactSet(output_dir, json_path, markdown_path, next_actions_path)


def build_phase3az_gap_analysis(*, reports_dir: Path = Path("reports")) -> dict[str, Any]:
    reports = _load_reports(reports_dir)
    gaps = _gap_rows(reports)
    phases = _phase_queue(gaps)
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AZ",
        "phase_version": PHASE_3AZ_VERSION,
        "mode": "REPORT_ONLY_POST_REFRESH_GAP_ANALYSIS",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "source_reports": {key: str(value["path"]) for key, value in reports.items()},
        "summary": _summary(gaps, phases),
        "gaps": gaps,
        "implementation_queue": phases,
        "recommended_next_action": _recommended_next_action(gaps, phases),
    }


def build_phase3az_r11_non_crypto_activation(
    *,
    reports_dir: Path = Path("reports"),
    weather_location_counts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    reports = _load_r11_reports(reports_dir)
    coverage = _payload(reports, "market_coverage")
    dashboard_truth = _payload(reports, "dashboard_truth")
    gap_analysis = _payload(reports, "phase3az")
    placeholder = _summary_payload(reports, "sports_placeholder_watch")
    dominant_weather_location = _dominant_weather_location(weather_location_counts or [])
    candidates = _r11_category_candidates(
        coverage=coverage,
        placeholder_summary=placeholder,
        dominant_weather_location=dominant_weather_location,
    )
    selected = candidates[0] if candidates else None
    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AZ-R11",
        "phase_version": PHASE_3AZ_R11_VERSION,
        "mode": "PAPER_ONLY_NON_CRYPTO_CATEGORY_ACTIVATION_SELECTOR",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "source_reports": {key: str(value["path"]) for key, value in reports.items()},
        "crypto_truth": _r11_crypto_truth(dashboard_truth),
        "summary": _r11_summary(
            candidates,
            selected=selected,
            gap_analysis=gap_analysis,
            dominant_weather_location=dominant_weather_location,
        ),
        "selected_category": selected,
        "category_candidates": candidates,
        "weather_location_counts": weather_location_counts or [],
        "recommended_sprint": _r11_recommended_sprint(selected),
        "operator_do_not_run": [
            "Do not run accelerate-learning from this selector.",
            "Do not create paper trades from this selector.",
            "Do not submit, cancel, replace, or amend live/demo exchange orders.",
            "Do not run normal link-remediate against KXMVE composites.",
        ],
    }


def _load_reports(reports_dir: Path) -> dict[str, dict[str, Any]]:
    paths = {
        "phase3ay": reports_dir / "phase3ay" / "phase3ay_health_refresh.json",
        "phase3ay_status": reports_dir / "phase3ay" / "phase3ay_status.json",
        "phase3aa": reports_dir / "phase3aa" / "phase3aa_outcome_realizer.json",
        "phase3aa_r2": reports_dir
        / "phase3aa_r2"
        / "phase3aa_r2_exact_settlement_harvest.json",
        "phase3aa_r3": reports_dir
        / "phase3aa_r3"
        / "phase3aa_r3_residual_settlement_audit.json",
        "phase3aa_r5": reports_dir
        / "phase3aa_r5"
        / "phase3aa_r5_closed_market_outcome_capture.json",
        "paper_settlement": reports_dir
        / "paper_settlement_reconciliation"
        / "paper_settlement_reconciliation.json",
        "market_coverage": reports_dir / "market_coverage" / "market_coverage_doctor.json",
        "phase3z_r2": reports_dir
        / "phase3z_r2"
        / "phase3z_r2_sports_provenance_repair.json",
        "phase3bb": reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        "phase3bb_r2": reports_dir
        / "phase3bb_r2"
        / "phase3bb_r2_general_candidate_routing.json",
        "phase3bb_r2_source_intake": reports_dir
        / "phase3bb_r2_sources"
        / "general_source_intake.json",
        "sports_placeholder_watch": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_placeholder_watch.json",
        "orchestrator": reports_dir / "phase_orchestrator.json",
    }
    return {
        key: {"path": path, "payload": _load_json(path), "exists": path.exists()}
        for key, path in paths.items()
    }


def _load_r11_reports(reports_dir: Path) -> dict[str, dict[str, Any]]:
    paths = {
        "market_coverage": reports_dir / "market_coverage" / "market_coverage_doctor.json",
        "dashboard_truth": reports_dir / "phase3aw" / "dashboard_truth.json",
        "phase3az": reports_dir / "phase3az" / "phase3az_gap_analysis.json",
        "phase3bb": reports_dir / "phase3bb" / "phase3bb_domain_readiness.json",
        "sports_placeholder_watch": reports_dir
        / "phase3ah_sports"
        / "phase3ah_sports_placeholder_watch.json",
    }
    return {
        key: {"path": path, "payload": _load_json(path), "exists": path.exists()}
        for key, path in paths.items()
    }


def _gap_rows(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    phase3ay = _payload(reports, "phase3ay")
    phase3aa = _payload(reports, "phase3aa")
    phase3aa_r2 = _summary_payload(reports, "phase3aa_r2")
    phase3aa_r3 = _summary_payload(reports, "phase3aa_r3")
    phase3aa_r5 = _summary_payload(reports, "phase3aa_r5")
    paper = _summary_payload(reports, "paper_settlement")
    coverage = _payload(reports, "market_coverage")
    phase3z_r2 = _payload(reports, "phase3z_r2")
    phase3bb = _payload(reports, "phase3bb")
    phase3bb_r2 = _payload(reports, "phase3bb_r2")
    phase3bb_r2_source_intake = _payload(reports, "phase3bb_r2_source_intake")
    placeholder = _summary_payload(reports, "sports_placeholder_watch")
    orchestrator = _payload(reports, "orchestrator")

    rows.extend(_missing_report_gaps(reports))
    rows.extend(
        _phase3bb_domain_gaps(
            phase3bb,
            phase3bb_r2=phase3bb_r2,
            phase3bb_r2_source_intake=phase3bb_r2_source_intake,
        )
    )

    phase3ay_summary = phase3ay.get("summary") or {}
    if int(phase3ay_summary.get("steps_error") or 0) > 0:
        rows.append(
            _gap(
                "phase3ay_step_errors",
                "HIGH",
                "Fix failed health-refresh steps before trusting downstream reports.",
                f"{phase3ay_summary.get('steps_error')} Phase 3AY step(s) errored.",
                "Inspect reports/phase3ay/phase3ay_health_refresh.json and stderr logs.",
                implementation_needed=True,
            )
        )

    eligible_after = int(phase3aa.get("eligible_after_realize") or 0)
    if eligible_after and not bool(phase3aa_r3.get("residue_cleared")):
        rows.append(
            _gap(
                "paper_realization_residue",
                "HIGH",
                "Some exact-settlement-eligible paper rows remain after realization.",
                (
                    f"{eligible_after} row(s) remain eligible after Phase 3AA realization. "
                    f"R3 classifications: {phase3aa_r3 or 'not yet run'}"
                ),
                "Run Phase 3AA-R3 residual exact settlement audit.",
                phase="3AA-R3",
                command=(
                    "kalshi-bot phase3aa-r3-residual-settlement-audit "
                    "--output-dir reports/phase3aa_r3"
                ),
                implementation_needed=True,
            )
        )

    due = int((phase3aa.get("eta_schedule") or {}).get("summary", {}).get("due_or_overdue") or 0)
    exact_written = int(phase3aa_r2.get("exact_settlements_written") or 0)
    if due and exact_written == 0:
        closed_without_outcome = int(phase3aa_r5.get("closed_without_outcome_rows") or 0)
        usable_candidates = int(phase3aa_r5.get("usable_outcome_candidate_rows") or 0)
        rows.append(
            _gap(
                "due_paper_without_new_exact_settlements",
                "MEDIUM",
                "Due paper trades still need exact-ticker settlement evidence.",
                _due_settlement_evidence(
                    due=due,
                    closed_without_outcome=closed_without_outcome,
                    usable_candidates=usable_candidates,
                ),
                _due_settlement_next_action(
                    closed_without_outcome=closed_without_outcome,
                    usable_candidates=usable_candidates,
                ),
                phase="3AY",
                command=(
                    "kalshi-bot phase3ay-health-refresh --all-markets "
                    "--cycles 1 --interval-seconds 0"
                ),
                implementation_needed=False,
            )
        )

    closed_without_outcome = int(phase3aa_r5.get("closed_without_outcome_rows") or 0)
    usable_candidates = int(phase3aa_r5.get("usable_outcome_candidate_rows") or 0)
    if closed_without_outcome and usable_candidates == 0:
        rows.append(
            _gap(
                "closed_markets_without_exposed_outcome",
                "MEDIUM",
                "Closed exact market payloads do not expose a supported outcome field.",
                (
                    f"Phase 3AA-R5 reviewed {closed_without_outcome} closed exact market "
                    "payload(s) and found 0 usable outcome candidate(s)."
                ),
                "Keep these rows blocked; continue exact-ticker watch/refresh only.",
                phase="3AA-R5",
                command=(
                    "kalshi-bot phase3aa-r5-closed-market-outcome-capture "
                    "--output-dir reports/phase3aa_r5"
                ),
                implementation_needed=False,
            )
        )

    unusable = int(phase3aa_r2.get("source_settled_without_usable_outcome") or 0)
    if unusable and not _residual_outcome_gap_cleared(
        phase3aa=phase3aa,
        phase3aa_r3=phase3aa_r3,
        paper=paper,
    ):
        rows.append(
            _gap(
                "settled_source_without_usable_outcome",
                "HIGH",
                "Exact market endpoint says closed/settled but lacks a usable result/value.",
                f"{unusable} exact ticker response(s) were settled without usable outcome fields.",
                "Build Phase 3AA-R3 source outcome field audit before changing settlement parsing.",
                phase="3AA-R3",
                command="kalshi-bot phase3az-gap-analysis --output-dir reports/phase3az",
                implementation_needed=True,
            )
        )

    fetch_errors = int(phase3aa_r2.get("fetch_errors") or 0)
    if fetch_errors:
        rows.append(
            _gap(
                "exact_ticker_fetch_errors",
                "MEDIUM",
                "Some exact ticker harvest GETs still fail.",
                f"{fetch_errors} exact ticker fetch error(s).",
                "Keep harvest retrying and review missing/expired ticker patterns.",
                phase="3AA-R2",
                command=(
                    "kalshi-bot phase3aa-r2-exact-settlement-harvest "
                    "--output-dir reports/phase3aa_r2"
                ),
                implementation_needed=False,
            )
        )

    if int(paper.get("sibling_different_contract_leg") or 0) or int(
        paper.get("validated_sibling_requires_review") or 0
    ):
        rows.append(
            _gap(
                "sibling_settlement_pressure",
                "LOW",
                "Sibling settlement candidates exist but remain blocked by exact-ticker policy.",
                (
                    f"different_leg={paper.get('sibling_different_contract_leg', 0)}, "
                    f"requires_review={paper.get('validated_sibling_requires_review', 0)}"
                ),
                (
                    "Preserve exact ticker policy; only add manual same-leg evidence "
                    "tooling if needed."
                ),
                implementation_needed=False,
            )
        )

    unhealthy_coverage = _unhealthy_coverage_rows(coverage)
    if unhealthy_coverage:
        diagnosed = _phase3z_r2_diagnosed_no_safe_rows(phase3z_r2)
        safe_rows = int((phase3z_r2.get("summary") or {}).get("rows_safe_to_repair") or 0)
        rows.append(
            _gap(
                "market_coverage_degraded",
                "HIGH" if not diagnosed else "MEDIUM",
                "Market coverage still has degraded producer-to-consumer rows.",
                _coverage_gap_evidence(
                    unhealthy_coverage,
                    phase3z_r2=phase3z_r2,
                    diagnosed=diagnosed,
                ),
                (
                    "Review Phase 3Z-R2 safe rows before Phase 3AE."
                    if safe_rows
                    else "Continue Phase 3AH schedule/roster evidence; no safe repair rows exist."
                ),
                phase="3AE" if safe_rows else "3AH",
                command=(
                    "kalshi-bot phase3ae-verified-sports-connector --output-dir reports/phase3ae"
                    if safe_rows
                    else (
                        "kalshi-bot phase3z-r2-sports-provenance-repair "
                        "--output-dir reports/phase3z_r2"
                    )
                ),
                implementation_needed=bool(safe_rows),
            )
        )

    if int(placeholder.get("still_placeholder_rows") or 0):
        rows.append(
            _gap(
                "sports_round_placeholders_block_phase3ae",
                "MEDIUM",
                "World Cup round placeholders still block safe Phase 3AE upgrades.",
                f"{placeholder.get('still_placeholder_rows')} placeholder row(s) still unresolved.",
                "Keep watching source schedules; do not treat placeholders as teams.",
                phase="3AH",
                command=(
                    "kalshi-bot phase3ah-sports-placeholder-watch "
                    "--output-dir reports/phase3ah_sports"
                ),
                implementation_needed=False,
            )
        )

    partial = _sports_partial_count(orchestrator=orchestrator, phase3z_r2=phase3z_r2)
    if partial:
        diagnosed = _phase3z_r2_diagnosed_no_safe_rows(phase3z_r2)
        rows.append(
            _gap(
                "sports_partial_provenance",
                "MEDIUM",
                "Sports links still include partial provenance that cannot safely upgrade.",
                _sports_partial_evidence(partial, phase3z_r2=phase3z_r2),
                (
                    "Continue Phase 3AH evidence gathering; Phase 3Z-R2 found no safe "
                    "repair rows."
                    if diagnosed
                    else "Use Phase 3AH/3AE only after team + time + market-type evidence is clean."
                ),
                phase="3AH/3AE",
                command=(
                    "kalshi-bot phase3ah-sports-placeholder-watch "
                    "--output-dir reports/phase3ah_sports"
                    if diagnosed
                    else "kalshi-bot phase3ag-sports-link-repair-pass --output-dir reports/phase3ag"
                ),
                implementation_needed=not diagnosed,
            )
        )

    return sorted(rows, key=lambda row: (_severity_rank(row["severity"]), row["gap_id"]))


def _phase3bb_domain_gaps(
    payload: dict[str, Any],
    *,
    phase3bb_r2: dict[str, Any],
    phase3bb_r2_source_intake: dict[str, Any],
) -> list[dict[str, Any]]:
    domain_rows = payload.get("domain_rows") if isinstance(payload, dict) else None
    if not isinstance(domain_rows, list):
        return []

    rows: list[dict[str, Any]] = []
    by_domain = {
        str(row.get("domain")): row
        for row in domain_rows
        if isinstance(row, dict) and row.get("domain")
    }
    general = by_domain.get("general")
    if isinstance(general, dict) and bool(general.get("actionable_now")):
        counts = general.get("counts") if isinstance(general.get("counts"), dict) else {}
        parsed_markets = int(counts.get("parsed_markets") or 0)
        if parsed_markets:
            source_evidence_ready = _phase3bb_r2_needs_source_evidence(phase3bb_r2)
            source_evidence_implemented = _phase3bb_r2_source_intake_satisfies_gap(
                phase3bb_r2_source_intake,
                expected_general_markets=parsed_markets,
            )
            if not (source_evidence_ready and source_evidence_implemented):
                r3_ready = (
                    False
                    if source_evidence_ready
                    else _phase3bb_r2_prefers_reclassification(phase3bb_r2)
                )
                objective = "General markets need taxonomy and candidate-routing work."
                next_action = (
                    "Build Phase 3BB-R2 to route general-market candidates into "
                    "economic/news/company/geopolitical buckets without creating "
                    "unsafe links."
                )
                if source_evidence_ready:
                    objective = (
                        "General structured candidates need paper-only source evidence "
                        "before link or forecast work."
                    )
                    next_action = (
                        "Build Phase 3BB-R2 source-evidence reports for commodity, "
                        "transportation, and infrastructure diagnostics. Keep all link, "
                        "feature, forecast, and trade writes blocked."
                    )
                elif r3_ready:
                    objective = "General sports/cross-category rows need reclassification review."
                    next_action = (
                        "Build Phase 3BB-R3 to group sports/cross-category leakage and "
                        "manual-review rows before any parser migration."
                    )
                rows.append(
                    _gap(
                        "general_domain_taxonomy_actionable",
                        "MEDIUM",
                        objective,
                        _general_domain_evidence(general),
                        next_action,
                        phase="3BB-R3" if r3_ready else "3BB-R2",
                        command=_phase3bb_general_command(
                            r3_ready,
                            source_evidence_ready=source_evidence_ready,
                        ),
                        implementation_needed=True,
                    )
                )

    waiting_domains = []
    for domain in ("economic", "news"):
        row = by_domain.get(domain)
        if not isinstance(row, dict) or bool(row.get("actionable_now")):
            continue
        status = str(row.get("status") or "")
        if status in {
            "WAITING_FOR_COMPATIBLE_MARKETS",
            "CONTEXT_READY_NO_ACTIVE_NEWS_MARKETS",
        }:
            waiting_domains.append(f"{domain}:{status}")
    if waiting_domains:
        rows.append(
            _gap(
                "economic_news_waiting_for_compatible_markets",
                "LOW",
                "Economic/news evidence exists but has no compatible parsed markets yet.",
                "; ".join(waiting_domains),
                "Keep refresh/source watches running; do not force links without parsed markets.",
                phase="3BB",
                command="kalshi-bot phase3bb-domain-readiness --output-dir reports/phase3bb",
                implementation_needed=False,
            )
        )
    return rows


def _dominant_weather_location(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = [
        {
            "location_key": str(row.get("location_key") or "").strip(),
            "link_count": _int_value(row.get("link_count")),
        }
        for row in rows
        if isinstance(row, dict) and str(row.get("location_key") or "").strip()
    ]
    if not normalized:
        return None
    return sorted(
        normalized,
        key=lambda row: (-int(row["link_count"]), str(row["location_key"])),
    )[0]


def _r11_category_candidates(
    *,
    coverage: dict[str, Any],
    placeholder_summary: dict[str, Any],
    dominant_weather_location: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = _coverage_category_rows(coverage)
    candidates = [
        _r11_candidate(
            row,
            placeholder_summary=placeholder_summary,
            dominant_weather_location=dominant_weather_location,
        )
        for row in rows
        if row.get("category") in {"weather", "sports", "economic", "news", "general"}
    ]
    return sorted(
        candidates,
        key=lambda row: (
            -int(row["score"]),
            str(row["category"]),
        ),
    )


def _coverage_category_rows(coverage: dict[str, Any]) -> list[dict[str, Any]]:
    dashboard = coverage.get("dashboard") if isinstance(coverage.get("dashboard"), dict) else {}
    dashboard_rows = dashboard.get("category_rows")
    if isinstance(dashboard_rows, list) and dashboard_rows:
        return [
            _normalize_category_row(row)
            for row in dashboard_rows
            if isinstance(row, dict)
        ]
    coverage_rows = coverage.get("coverage_rows")
    if isinstance(coverage_rows, list):
        return [
            _normalize_category_row(row)
            for row in coverage_rows
            if isinstance(row, dict)
        ]
    return []


def _normalize_category_row(row: dict[str, Any]) -> dict[str, Any]:
    category = str(row.get("category") or row.get("scope_key") or row.get("scope") or "")
    coverage_value = row.get("coverage")
    coverage_percent = row.get("coverage_percent")
    if coverage_value is None and isinstance(coverage_percent, str):
        stripped = coverage_percent.strip().rstrip("%")
        try:
            coverage_value = float(stripped) / 100
        except ValueError:
            coverage_value = None
    return {
        "category": category,
        "status": str(row.get("status") or row.get("health") or row.get("status_label") or ""),
        "parsed_markets": _int_value(row.get("parsed_markets")),
        "parsed_legs": _int_value(row.get("parsed_legs")),
        "linkable_markets": _int_value(row.get("linkable_markets") or row.get("coverage_denominator")),
        "linked_markets": _int_value(row.get("linked_markets") or row.get("usable_markets")),
        "derived_markets": _int_value(row.get("derived_markets") or row.get("derived_usable_markets")),
        "verified_markets": _int_value(row.get("verified_schedule_markets")),
        "partial_markets": _int_value(row.get("partial_markets")),
        "partial_link_rows": _int_value(row.get("partial_link_rows")),
        "unlinked_markets": _int_value(row.get("unlinked_markets")),
        "unsupported_multileg_markets": _int_value(row.get("unsupported_multileg_markets")),
        "coverage": _float_value(coverage_value),
        "next_action": row.get("next_action"),
    }


def _r11_candidate(
    row: dict[str, Any],
    *,
    placeholder_summary: dict[str, Any],
    dominant_weather_location: dict[str, Any] | None,
) -> dict[str, Any]:
    category = str(row.get("category") or "")
    parsed = int(row.get("parsed_markets") or 0)
    linkable = int(row.get("linkable_markets") or 0)
    linked = int(row.get("linked_markets") or 0)
    coverage = row.get("coverage")
    coverage_ratio = float(coverage) if isinstance(coverage, (int, float)) else None
    partial = int(row.get("partial_markets") or 0)
    unsupported = int(row.get("unsupported_multileg_markets") or 0)
    derived = int(row.get("derived_markets") or 0)
    placeholders = (
        int(placeholder_summary.get("still_placeholder_rows") or 0)
        if category == "sports"
        else 0
    )
    blockers = _r11_blockers(
        category=category,
        parsed=parsed,
        linkable=linkable,
        linked=linked,
        coverage=coverage_ratio,
        partial=partial,
        unsupported=unsupported,
        derived=derived,
        placeholders=placeholders,
    )
    activation_state = _r11_activation_state(
        category=category,
        parsed=parsed,
        linkable=linkable,
        linked=linked,
        coverage=coverage_ratio,
        blockers=blockers,
    )
    score = _r11_score(
        category=category,
        parsed=parsed,
        linkable=linkable,
        linked=linked,
        coverage=coverage_ratio,
        blockers=blockers,
        unsupported=unsupported,
        derived=derived,
        placeholders=placeholders,
    )
    candidate = {
        "category": category,
        "activation_state": activation_state,
        "score": score,
        "parsed_markets": parsed,
        "linked_markets": linked,
        "linkable_markets": linkable,
        "coverage_percent": _format_percent(coverage_ratio),
        "derived_markets": derived,
        "verified_markets": int(row.get("verified_markets") or 0),
        "partial_markets": partial,
        "partial_link_rows": int(row.get("partial_link_rows") or 0),
        "unsupported_multileg_markets": unsupported,
        "blockers": blockers,
        "reason": _r11_reason(
            category=category,
            activation_state=activation_state,
            parsed=parsed,
            linked=linked,
            linkable=linkable,
            coverage=coverage_ratio,
            unsupported=unsupported,
            derived=derived,
            placeholders=placeholders,
        ),
    }
    if category == "weather" and dominant_weather_location:
        candidate["activation_location_key"] = dominant_weather_location.get("location_key")
        candidate["activation_location_link_count"] = _int_value(
            dominant_weather_location.get("link_count")
        )
    return candidate


def _r11_blockers(
    *,
    category: str,
    parsed: int,
    linkable: int,
    linked: int,
    coverage: float | None,
    partial: int,
    unsupported: int,
    derived: int,
    placeholders: int,
) -> list[str]:
    blockers: list[str] = []
    if parsed <= 0 or linkable <= 0:
        blockers.append("NO_COMPATIBLE_PARSED_MARKETS")
    if linkable > 0 and linked < linkable:
        blockers.append("LINK_COVERAGE_INCOMPLETE")
    if coverage is not None and coverage < 1:
        blockers.append("COVERAGE_BELOW_100_PERCENT")
    if partial > 0:
        blockers.append("PARTIAL_MARKETS_REMAIN")
    if unsupported > 0:
        blockers.append("UNSUPPORTED_KXMVE_COMPOSITES_PARKED")
    if category == "sports" and derived > 0:
        blockers.append("SPORTS_DERIVED_PROVENANCE_NEEDS_VERIFICATION")
    if category == "sports" and placeholders > 0:
        blockers.append("SPORTS_ROUND_PLACEHOLDERS_REMAIN")
    if category == "general":
        blockers.append("NO_SPECIALIZED_GENERAL_LINKER")
    return blockers


def _r11_activation_state(
    *,
    category: str,
    parsed: int,
    linkable: int,
    linked: int,
    coverage: float | None,
    blockers: list[str],
) -> str:
    if parsed <= 0 or linkable <= 0:
        return "WAITING_FOR_COMPATIBLE_MARKETS"
    if category == "weather" and linked >= linkable and coverage == 1 and not blockers:
        return "READY_FOR_WEATHER_ACTIVATION"
    if category == "sports":
        return "PROVENANCE_REPAIR_BEFORE_TRADING"
    if category in {"economic", "news"} and linked >= linkable and coverage == 1:
        return "SOURCE_PIPELINE_REVIEW_READY"
    if category == "general":
        return "CONTEXT_ONLY_UNSPECIALIZED"
    return "REVIEW_REQUIRED"


def _r11_score(
    *,
    category: str,
    parsed: int,
    linkable: int,
    linked: int,
    coverage: float | None,
    blockers: list[str],
    unsupported: int,
    derived: int,
    placeholders: int,
) -> int:
    category_base = {
        "weather": 80,
        "economic": 50,
        "news": 45,
        "sports": 30,
        "general": 5,
    }.get(category, 0)
    score = category_base
    if parsed > 0:
        score += 50
    if linkable > 0 and linked >= linkable and coverage == 1:
        score += 60
    if not blockers:
        score += 60
    if category == "weather":
        score += 40
    score += min(parsed, 1000) // 20
    if "NO_COMPATIBLE_PARSED_MARKETS" in blockers:
        score -= 100
    if "LINK_COVERAGE_INCOMPLETE" in blockers:
        score -= 60
    if unsupported:
        score -= 80
    if derived:
        score -= 30
    if placeholders:
        score -= 50
    return score


def _r11_reason(
    *,
    category: str,
    activation_state: str,
    parsed: int,
    linked: int,
    linkable: int,
    coverage: float | None,
    unsupported: int,
    derived: int,
    placeholders: int,
) -> str:
    coverage_text = _format_percent(coverage)
    if activation_state == "READY_FOR_WEATHER_ACTIVATION":
        return (
            f"Weather has {parsed} parsed market(s), {linked}/{linkable} linked "
            f"market(s), {coverage_text} coverage, and no parked composites."
        )
    if category == "sports":
        return (
            "Sports has broad coverage but remains a slower activation lane because "
            f"{derived} market(s) are derived, {unsupported} KXMVE composite market(s) "
            f"are parked, and {placeholders} placeholder row(s) still need source evidence."
        )
    if category in {"economic", "news"}:
        return (
            f"{category} currently has {parsed} parsed market(s); wait for compatible "
            "markets before parser/linker or forecast work."
        )
    if category == "general":
        return "General remains market context until a specialized source/linking lane exists."
    return f"{category} needs operator review before activation."


def _r11_recommended_sprint(selected: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not selected:
        return []
    category = selected.get("category")
    if category == "weather" and selected.get("activation_state") == "READY_FOR_WEATHER_ACTIVATION":
        location_key = str(selected.get("activation_location_key") or "kansas_city")
        return [
            _r11_sprint_step(
                1,
                "weather_source_refresh",
                f"kalshi-bot ingest-weather --location-key {location_key}",
                f"Refresh weather observations/forecasts for the dominant linked {location_key} lane.",
            ),
            _r11_sprint_step(
                2,
                "weather_feature_build",
                f"kalshi-bot build-weather-features --location-key {location_key}",
                "Convert fresh forecasts into weather_v2 feature rows.",
            ),
            _r11_sprint_step(
                3,
                "weather_market_link_refresh",
                "kalshi-bot link-weather-markets",
                "Refresh weather market links after the latest catalog/feature state.",
            ),
            _r11_sprint_step(
                4,
                "weather_forecast",
                "kalshi-bot forecast --model weather_v2 --limit 500",
                "Generate weather_v2 forecasts for the latest weather-linked snapshots.",
            ),
            _r11_sprint_step(
                5,
                "weather_ranking_report",
                "kalshi-bot market-rankings --limit 100 --output reports/weather_market_rankings.md",
                "Rank current opportunities after weather forecasts are present.",
            ),
            _r11_sprint_step(
                6,
                "paper_ready_gate",
                (
                    "kalshi-bot phase3ap-paper-ready-unblock-report "
                    "--output-dir reports/phase3ap --reports-dir reports"
                ),
                "Confirm whether any weather row becomes paper-ready; do not create trades here.",
            ),
        ]
    return [
        _r11_sprint_step(
            1,
            "keep_report_only",
            "kalshi-bot phase3az-r11-non-crypto-category-activation --output-dir reports/phase3az_r11 --reports-dir reports",
            "No category is ready for a source-to-paper sprint yet; rerun after fresh evidence.",
        )
    ]


def _r11_sprint_step(priority: int, step_id: str, command: str, purpose: str) -> dict[str, Any]:
    return {
        "priority": priority,
        "step_id": step_id,
        "command": command,
        "purpose": purpose,
        "safety": "PAPER_ONLY_OPERATOR_REVIEW_BEFORE_TRADES",
    }


def _r11_crypto_truth(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    funnel = (
        payload.get("current_crypto_funnel")
        if isinstance(payload.get("current_crypto_funnel"), dict)
        else {}
    )
    return {
        "r5_running": bool(summary.get("r5_running") or funnel.get("r5_running")),
        "r5_stale_report": bool(summary.get("r5_stale_report") or funnel.get("r5_stale_report")),
        "snapshots_fresh": bool(summary.get("snapshots_fresh") or funnel.get("snapshots_fresh")),
        "forecasts_fresh": bool(summary.get("forecasts_fresh") or funnel.get("forecasts_fresh")),
        "rankings_fresh": bool(summary.get("rankings_fresh") or funnel.get("rankings_fresh")),
        "paper_ready_candidates": _int_value(
            summary.get("paper_ready_candidates") or funnel.get("paper_ready_candidates")
        ),
        "positive_ev_rows": _int_value(
            summary.get("current_positive_ev_rows") or funnel.get("current_positive_ev_rows")
        ),
        "true_current_blocker": summary.get("true_current_blocker")
        or funnel.get("true_current_blocker"),
    }


def _r11_summary(
    candidates: list[dict[str, Any]],
    *,
    selected: dict[str, Any] | None,
    gap_analysis: dict[str, Any],
    dominant_weather_location: dict[str, Any] | None,
) -> dict[str, Any]:
    gap_summary = gap_analysis.get("summary") if isinstance(gap_analysis.get("summary"), dict) else {}
    return {
        "candidate_count": len(candidates),
        "selected_category": selected.get("category") if selected else None,
        "selected_activation_state": selected.get("activation_state") if selected else None,
        "selected_score": selected.get("score") if selected else None,
        "weather_ready": any(
            row.get("category") == "weather"
            and row.get("activation_state") == "READY_FOR_WEATHER_ACTIVATION"
            for row in candidates
        ),
        "dominant_weather_location_key": (
            dominant_weather_location.get("location_key")
            if isinstance(dominant_weather_location, dict)
            else None
        ),
        "dominant_weather_location_link_count": _int_value(
            dominant_weather_location.get("link_count")
            if isinstance(dominant_weather_location, dict)
            else 0
        ),
        "gap_analysis_top_gap": gap_summary.get("top_gap"),
        "gap_analysis_implementation_needed_count": _int_value(
            gap_summary.get("implementation_needed_count")
        ),
    }


def _render_r11_markdown(payload: dict[str, Any]) -> str:
    selected = payload.get("selected_category") if isinstance(payload.get("selected_category"), dict) else {}
    lines = [
        "# Phase 3AZ-R11 Non-Crypto Category Activation Sprint",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Summary",
        "",
    ]
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Selected Category",
            "",
            f"- category: {selected.get('category')}",
            f"- activation_state: {selected.get('activation_state')}",
            f"- activation_location_key: {selected.get('activation_location_key')}",
            f"- activation_location_link_count: {selected.get('activation_location_link_count')}",
            f"- score: {selected.get('score')}",
            f"- reason: {selected.get('reason')}",
            "",
            "## Category Candidates",
            "",
            "| Category | State | Score | Parsed | Linked/Linkable | Coverage | Blockers |",
            "| --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for row in payload.get("category_candidates") or []:
        blockers = ", ".join(row.get("blockers") or []) or "none"
        lines.append(
            "| {category} | {state} | {score} | {parsed} | {linked}/{linkable} | {coverage} | {blockers} |".format(
                category=row.get("category"),
                state=row.get("activation_state"),
                score=row.get("score"),
                parsed=row.get("parsed_markets"),
                linked=row.get("linked_markets"),
                linkable=row.get("linkable_markets"),
                coverage=row.get("coverage_percent"),
                blockers=blockers,
            )
        )
    lines.extend(["", "## Recommended Sprint", ""])
    for step in payload.get("recommended_sprint") or []:
        lines.append(f"{step['priority']}. `{step['command']}`")
        lines.append(f"   - {step['purpose']}")
    lines.extend(["", "## Do Not Run", ""])
    for item in payload.get("operator_do_not_run") or []:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _render_r11_next_actions(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AZ-R11 Next Actions",
        "",
        "Run only after confirming crypto dashboard truth is not stale.",
        "",
    ]
    for step in payload.get("recommended_sprint") or []:
        lines.append(f"- `{step['command']}`")
    return "\n".join(lines) + "\n"


def _format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _phase3bb_r2_source_intake_satisfies_gap(
    payload: dict[str, Any],
    *,
    expected_general_markets: int,
) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if payload.get("safety_mode") != "REPORT_ONLY_NO_WRITES":
        return False
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if int(summary.get("general_markets_reviewed") or 0) < expected_general_markets:
        return False
    if int(summary.get("active_general_markets_reviewed") or 0) <= 0:
        return False
    taxonomy_counts = summary.get("taxonomy_counts")
    if not isinstance(taxonomy_counts, dict):
        return False
    required_buckets = {
        "COMMODITY_PRICE_CANDIDATE",
        "TRANSPORTATION_OPERATION_CANDIDATE",
        "INFRASTRUCTURE_CAPACITY_CANDIDATE",
        "SPORTS_OR_CROSS_CATEGORY_LEAKAGE",
        "GENERAL_UNCLASSIFIED",
    }
    if not required_buckets.intersection(taxonomy_counts):
        return False
    blocked_flags = (
        "link_writes",
        "feature_writes",
        "forecast_writes",
        "opportunity_writes",
        "paper_trade_writes",
        "settlement_writes",
        "live_or_demo_execution",
    )
    if any(bool(summary.get(flag)) for flag in blocked_flags):
        return False
    safety_gate = payload.get("safety_gate")
    if isinstance(safety_gate, dict):
        gate_flags = (
            "writes_database",
            "writes_links",
            "writes_features",
            "writes_forecasts",
            "writes_opportunities",
            "places_paper_orders",
            "settles_trades",
            "places_demo_orders",
            "places_live_orders",
        )
        if any(bool(safety_gate.get(flag)) for flag in gate_flags):
            return False
    return all(
        isinstance(payload.get(key), expected_type) and bool(payload.get(key))
        for key, expected_type in (
            ("taxonomy_review_rows", list),
            ("source_evidence_requirements", list),
            ("source_readiness_matrix", list),
            ("candidate_market_samples", dict),
            ("next_actions", list),
        )
    )


def _phase3bb_r2_prefers_reclassification(payload: dict[str, Any]) -> bool:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        return False
    buckets = summary.get("candidate_buckets")
    if not isinstance(buckets, dict):
        return False
    if (
        int(buckets.get("economic") or 0)
        or int(buckets.get("news") or 0)
        or int(buckets.get("operational_or_commodity") or 0)
    ):
        return False
    return int(buckets.get("sports_or_cross_category_leakage") or 0) > 0


def _phase3bb_r2_needs_source_evidence(payload: dict[str, Any]) -> bool:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        return False
    diagnostics = summary.get("general_signal_diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    if int(diagnostics.get("diagnostic_rows") or 0) <= 0:
        return False
    if int(diagnostics.get("safe_to_forecast_rows") or 0) > 0:
        return False
    readiness = diagnostics.get("readiness_counts")
    if isinstance(readiness, dict) and int(readiness.get("SOURCE_DESIGN_REQUIRED") or 0):
        return True
    adapters = diagnostics.get("source_adapter_counts")
    return isinstance(adapters, dict) and bool(adapters)


def _phase3bb_general_command(
    r3_ready: bool,
    *,
    source_evidence_ready: bool = False,
) -> str:
    if r3_ready:
        return "kalshi-bot phase3bb-r3-general-reclassification --output-dir reports/phase3bb_r3"
    if source_evidence_ready:
        return (
            "kalshi-bot phase3bb-r2-general-source-intake "
            "--output-dir reports/phase3bb_r2_sources"
        )
    return "kalshi-bot phase3bb-r2-general-candidate-routing --output-dir reports/phase3bb_r2"


def _general_domain_evidence(row: dict[str, Any]) -> str:
    counts = row.get("counts") if isinstance(row.get("counts"), dict) else {}
    taxonomy = row.get("taxonomy_counts") if isinstance(row.get("taxonomy_counts"), dict) else {}
    taxonomy_bits = [
        f"{key}={value}"
        for key, value in sorted(taxonomy.items(), key=lambda item: str(item[0]))
    ]
    return (
        f"general parsed_markets={counts.get('parsed_markets', 0)}, "
        f"active_parsed_markets={counts.get('active_parsed_markets', 0)}, "
        f"taxonomy: {', '.join(taxonomy_bits) if taxonomy_bits else 'none'}"
    )


def _due_settlement_evidence(
    *,
    due: int,
    closed_without_outcome: int,
    usable_candidates: int,
) -> str:
    if closed_without_outcome and usable_candidates == 0:
        return (
            f"{due} due/overdue trade(s), no newly written exact settlements. "
            f"Phase 3AA-R5 found {closed_without_outcome} closed exact payload(s) "
            "with no supported outcome field."
        )
    return f"{due} due/overdue trade(s), no newly written exact settlements."


def _due_settlement_next_action(
    *,
    closed_without_outcome: int,
    usable_candidates: int,
) -> str:
    if closed_without_outcome and usable_candidates == 0:
        return (
            "Keep exact-ticker watch active; do not rerun realization or use sibling "
            "tickers until source outcomes appear."
        )
    if usable_candidates:
        return "Rerun Phase 3AA-R2, then dry-run Phase 3AA realization."
    return "Keep Phase 3AY running; do not settle from sibling tickers."


def _residual_outcome_gap_cleared(
    *,
    phase3aa: dict[str, Any],
    phase3aa_r3: dict[str, Any],
    paper: dict[str, Any],
) -> bool:
    if not bool(phase3aa_r3.get("residue_cleared")):
        return False
    if int(phase3aa_r3.get("residual_rows") or 0):
        return False
    if int(paper.get("eligible_to_settle_now") or 0):
        return False
    if int(phase3aa.get("eligible_after_realize") or 0):
        return False
    return True


def _phase3z_r2_diagnosed_no_safe_rows(payload: dict[str, Any]) -> bool:
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if not isinstance(summary, dict):
        return False
    if int(summary.get("rows_reviewed") or 0) <= 0:
        return False
    return int(summary.get("rows_safe_to_repair") or 0) == 0


def _sports_partial_count(
    *,
    orchestrator: dict[str, Any],
    phase3z_r2: dict[str, Any],
) -> int:
    summary = phase3z_r2.get("summary") if isinstance(phase3z_r2, dict) else None
    if isinstance(summary, dict) and summary.get("partial_legacy_markets") is not None:
        return int(summary.get("partial_legacy_markets") or 0)
    sports = (orchestrator.get("evidence") or {}).get("sports_provenance") or {}
    return int(sports.get("partial_without_upgrade") or 0)


def _coverage_gap_evidence(
    unhealthy_rows: list[dict[str, Any]],
    *,
    phase3z_r2: dict[str, Any],
    diagnosed: bool,
) -> str:
    if not diagnosed:
        return f"{len(unhealthy_rows)} coverage row(s) are not healthy."
    summary = phase3z_r2.get("summary") or {}
    return (
        f"{len(unhealthy_rows)} coverage row(s) are not healthy. Phase 3Z-R2 reviewed "
        f"{summary.get('rows_reviewed', 0)} sports degradation row(s), found "
        f"{summary.get('rows_safe_to_repair', 0)} safe repair row(s), and kept "
        f"{summary.get('placeholder_blocked_rows', 0)} placeholder row(s) blocked."
    )


def _sports_partial_evidence(partial: int, *, phase3z_r2: dict[str, Any]) -> str:
    summary = phase3z_r2.get("summary") if isinstance(phase3z_r2, dict) else None
    if not isinstance(summary, dict):
        return f"{partial} sports partial link(s) without upgrade."
    return (
        f"{partial} sports partial market(s) without upgrade. Phase 3Z-R2 reconciled "
        f"{summary.get('partial_legacy_markets', partial)} distinct partial market(s), "
        f"{summary.get('partial_legacy_link_rows', 0)} partial link row(s), and "
        f"{summary.get('unlinked_parsed_markets', 0)} unlinked parsed sports market(s)."
    )


def _missing_report_gaps(reports: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, report in reports.items():
        if key in {
            "phase3z_r2",
            "phase3aa_r5",
            "phase3bb",
            "phase3bb_r2",
            "phase3bb_r2_source_intake",
        }:
            continue
        if not report["exists"]:
            rows.append(
                _gap(
                    f"missing_{key}_report",
                    "HIGH",
                    "Expected report is missing.",
                    f"Missing {report['path']}.",
                    "Run the owning phase before trusting gap analysis.",
                    implementation_needed=False,
                )
            )
    return rows


def _phase_queue(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue = []
    for gap in gaps:
        if not gap["implementation_needed"]:
            continue
        queue.append(
            {
                "phase": gap.get("phase") or "NEXT",
                "gap_id": gap["gap_id"],
                "priority": gap["severity"],
                "objective": gap["title"],
                "starter_command": gap.get("command"),
                "safety": "PAPER_ONLY_NO_LIVE_OR_DEMO_EXECUTION",
            }
        )
    return queue


def _summary(gaps: list[dict[str, Any]], phases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "gap_count": len(gaps),
        "high_gaps": sum(1 for gap in gaps if gap["severity"] == "HIGH"),
        "medium_gaps": sum(1 for gap in gaps if gap["severity"] == "MEDIUM"),
        "low_gaps": sum(1 for gap in gaps if gap["severity"] == "LOW"),
        "implementation_needed_count": len(phases),
        "top_gap": gaps[0]["gap_id"] if gaps else None,
        "top_phase": phases[0]["phase"] if phases else None,
    }


def _recommended_next_action(
    gaps: list[dict[str, Any]],
    phases: list[dict[str, Any]],
) -> str:
    if not gaps:
        return "No current gaps found in report artifacts."
    if phases:
        top = phases[0]
        return f"Implement {top['phase']} for {top['gap_id']} next."
    return "No implementation gap is currently actionable; keep the refresh/watch loops running."


def _gap(
    gap_id: str,
    severity: str,
    title: str,
    evidence: str,
    next_action: str,
    *,
    phase: str | None = None,
    command: str | None = None,
    implementation_needed: bool,
) -> dict[str, Any]:
    return {
        "gap_id": gap_id,
        "severity": severity,
        "title": title,
        "evidence": evidence,
        "phase": phase,
        "command": command,
        "implementation_needed": implementation_needed,
        "next_action": next_action,
    }


def _severity_rank(severity: str) -> int:
    return {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(severity, 3)


def _payload(reports: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    payload = reports.get(key, {}).get("payload")
    return payload if isinstance(payload, dict) else {}


def _summary_payload(reports: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    payload = _payload(reports, key)
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def _unhealthy_coverage_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    healthy = {"HEALTHY", "NO_COMPATIBLE_ACTIVE_MARKETS"}
    rows = payload.get("coverage_rows") or []
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("health") not in healthy
    ]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 3AZ Post-Refresh Gap Analysis",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        "",
        "## Summary",
        "",
    ]
    for key, value in payload["summary"].items():
        if value is None:
            value = "none"
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Gaps",
            "",
            "| Severity | Gap | Implementation | Evidence | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for gap in payload["gaps"]:
        lines.append(
            f"| {gap['severity']} | {_md(gap['gap_id'])} | "
            f"{gap['implementation_needed']} | {_md(gap['evidence'])} | "
            f"{_md(gap['next_action'])} |"
        )
    if not payload["gaps"]:
        lines.append("| n/a | none | False | No gaps found. |  |")
    lines.extend(
        [
            "",
            "## Implementation Queue",
            "",
            "| Priority | Phase | Gap | Starter command |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in payload["implementation_queue"]:
        lines.append(
            f"| {row['priority']} | {_md(row['phase'])} | {_md(row['gap_id'])} | "
            f"`{_md(row.get('starter_command'))}` |"
        )
    if not payload["implementation_queue"]:
        lines.append("| n/a | none | none |  |")
    lines.extend(
        [
            "",
            "## Recommended Next Action",
            "",
            payload["recommended_next_action"],
            "",
        ]
    )
    return "\n".join(lines)


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")
