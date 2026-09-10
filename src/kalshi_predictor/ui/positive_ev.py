"""Bounded read-only view of independent research; never grants paper authority."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

MAX_BYTES = 2 * 1024 * 1024


def read_research(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("oversized")
        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("schema") != "independent-research-v1":
            raise ValueError("schema")
        rows = result.get("rows")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError("rows")
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("row")
        generated = datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError("clock")
        age = ((now or datetime.now(UTC)) - generated).total_seconds()
        result["fresh"] = 0 <= age <= 900
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return {"fresh": False, "rows": [], "primary_blocker": "Research report unavailable"}


def render_research(report: dict[str, Any]) -> str:
    def text(value: Any) -> str:
        return escape("Unknown" if value is None else str(value))

    metrics = report.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    cards = "".join(
        f"<div><strong>{label}</strong><p>{text(metrics.get(key))}</p></div>"
        for key, label in (
            ("markets_scanned", "Markets scanned"),
            ("independent_forecasts", "Independent forecasts"),
            ("positive_gross_ev", "Positive gross EV"),
            ("positive_net_ev", "Positive net EV"),
            ("shadow_eligible", "Shadow eligible"),
            ("paper_eligible", "Paper eligible"),
            ("open_local_paper", "Open local paper"),
            ("settled_local_paper", "Settled local paper"),
        )
    )
    fields = (
        ("ticker", "Market"), ("category", "Category"), ("side", "Side"), ("model", "Model"),
        ("independent_probability", "Independent probability"),
        ("executable_price", "Executable price"), ("gross_edge", "Gross edge"),
        ("fees", "Fees"), ("slippage", "Slippage"), ("uncertainty", "Uncertainty"),
        ("net_ev", "Net EV"), ("settlement_eta", "Settlement ETA"),
        ("student_t_gross_edge", "Student-t gross stress"),
        ("data_sources", "Data sources"), ("first_blocker", "First blocker"),
    )
    header = "".join(f"<th>{label}</th>" for _, label in fields)
    rows = "".join(
        "<tr>" + "".join(f"<td>{text(row.get(key))}</td>" for key, _ in fields) + "</tr>"
        for row in report.get("rows", [])[:100]
    )
    status = "Recent research snapshot" if report.get("fresh") else "Stale or unavailable snapshot"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Positive EV research</title>"
        "<style>body{font:16px system-ui;margin:2rem;color:#172535;background:#f5f7fa}"
        ".metrics{display:flex;flex-wrap:wrap;gap:1rem}.metrics div{background:white;padding:1rem}"
        "table{border-collapse:collapse;background:white}"
        "td,th{padding:.7rem;border:1px solid #ccd5df}"
        ".table{overflow:auto}p{line-height:1.5}</style></head><body>"
        f"<h1>Positive EV research</h1><p>{status}. As of {text(report.get('generated_at'))}.</p>"
        "<p>Gross-edge counts include uncalibrated research proxies. "
        "Reported estimates require source, cost, model and risk validation. "
        "This page cannot submit orders or authorize local paper positions.</p>"
        f"<div class='metrics'>{cards}</div>"
        f"<p>Top opportunity: {text(report.get('top_opportunity'))}</p>"
        f"<p>Primary blocker: {text(report.get('primary_blocker'))}</p>"
        f"<div class='table'><table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"
        "</div></body></html>"
    )


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/positive-ev", response_class=HTMLResponse)
    def positive_ev() -> HTMLResponse:
        path = Path(os.environ.get("POSITIVE_EV_REPORT_PATH", "reports/positive_ev/current.json"))
        return HTMLResponse(render_research(read_research(path)))

    return router
