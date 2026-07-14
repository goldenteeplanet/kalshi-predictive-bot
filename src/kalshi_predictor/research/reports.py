from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from kalshi_predictor.research.evidence import top_opportunity_evidence
from kalshi_predictor.research.narratives import generate_narrative, render_research_markdown
from kalshi_predictor.research.repository import store_opportunity_research_snapshot
from kalshi_predictor.utils.time import utc_now


def generate_research_report(
    session: Session,
    *,
    model_name: str,
    limit: int,
    output_path: str | Path,
) -> Path:
    rows = []
    for evidence in top_opportunity_evidence(session, model_name=model_name, limit=limit):
        narrative = generate_narrative(evidence)
        store_opportunity_research_snapshot(session, evidence=evidence, narrative=narrative)
        rows.append({"evidence": evidence, "narrative": narrative})
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render_report(rows, model_name=model_name), encoding="utf-8")
    return output


def _render_report(rows: list[dict[str, Any]], *, model_name: str) -> str:
    top_risks = _unique(
        risk
        for row in rows
        for risk in row["evidence"].get("risk_factors", [])
    )
    missing_data = _unique(
        missing
        for row in rows
        for missing in row["evidence"].get("missing_data", [])
    )
    model_drivers = _unique(
        str(row["narrative"].get("primary_driver"))
        for row in rows
        if row["narrative"].get("primary_driver")
    )
    lines = [
        "# Research Assistant Report",
        "",
        f"- Generated at: {utc_now().isoformat()}",
        f"- Model: `{model_name}`",
        "- Mode: PAPER / DEMO ONLY",
        "- External LLM calls: none",
        "",
        "## Executive Summary",
        "",
        f"- Opportunities explained: {len(rows)}",
        f"- Top risks tracked: {len(top_risks)}",
        f"- Missing data warnings: {len(missing_data)}",
        "",
        "## Top Opportunities Explained",
        "",
    ]
    if not rows:
        lines.append("No ranked opportunities are available yet.")
    for row in rows:
        lines.append(render_research_markdown(row["evidence"], row["narrative"]))
    lines.extend(["", "## Top Risks", ""])
    lines.extend(_bullet_lines(top_risks, empty="No major risks detected."))
    lines.extend(["", "## Model Drivers", ""])
    lines.extend(_bullet_lines(model_drivers, empty="No model drivers available yet."))
    lines.extend(["", "## Missing Data", ""])
    lines.extend(_bullet_lines(missing_data, empty="No major data gaps detected."))
    lines.extend(
        [
            "",
            "## Recommended Next Actions",
            "",
            "- Refresh collection, forecasts, and opportunity rankings before acting.",
            "- Use paper trading or demo dry-runs only.",
            "- Review wide-spread, low-liquidity, stale-data, and missing-backtest warnings.",
            "",
            "## Reminder: paper/demo only",
            "",
            "This report is deterministic local analysis. It is not live-trading advice and it "
            "does not enable real-money execution.",
            "",
        ]
    )
    return "\n".join(lines)


def _bullet_lines(values: list[str], *, empty: str) -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {value}" for value in values]


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique

