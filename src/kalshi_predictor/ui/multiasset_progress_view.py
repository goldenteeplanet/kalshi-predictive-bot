"""Render bounded root-published cohort audit snapshots, never execution authority."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

TOOL_SHA = "1f2abb79f5b1ee0f7b62a7893bcf815ccd8db41ce46d7d8f9fb411f2ce5942b1"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")
MAX_BYTES = 256_000


def _trusted(path: Path) -> bool:
    return all(
        not part.is_symlink()
        and part.stat().st_uid == 0
        and part.stat().st_mode & 0o022 == 0
        for part in (path, *path.parents)
    )


def _clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("aware audit time required")
    return parsed


def read_progress(directory: Path, *, now: datetime | None = None) -> dict[str, Any]:
    unavailable: dict[str, Any] = {"available": False}
    try:
        if not _trusted(directory) or not directory.is_dir():
            return unavailable
        candidates = []
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 4096:
                    raise ValueError("directory bound")
                if entry.name.startswith("combined-cohort-progress-wave-") and entry.name.endswith(
                    ".json"
                ):
                    candidates.append(Path(entry.path))
                    if len(candidates) > 32:
                        raise ValueError("snapshot bound")
        if not candidates:
            return unavailable
        path = max(candidates, key=lambda candidate: candidate.lstat().st_mtime_ns)
        if not _trusted(path) or not path.is_file():
            return unavailable
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return unavailable
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("scope") != "DEDUPLICATED_PROGRESS_WITH_SEPARATE_SOURCE_STRATA"
            or data.get("tool_sha256") != TOOL_SHA
            or data.get("execution_authority") is not False
            or data.get("independent_event_n") is not None
            or data.get("pooled_model_metrics") is not None
        ):
            return unavailable
        current = now or datetime.now(UTC)
        published = _clock(data["at"])
        if current.utcoffset() is None or published > current:
            return unavailable
        selected = data["selected_events"]
        if not isinstance(selected, list) or not 0 <= len(selected) <= 36:
            return unavailable
        seen: set[str] = set()
        by_asset = {asset: {"captured": 0, "evaluated": 0} for asset in ASSETS}
        evaluated = decisions = 0
        for event in selected:
            asset = event["asset"]
            if (
                asset not in ASSETS
                or type(event["event"]) is not str
                or not event["event"]
                or event["event"] in seen
                or event["capture"] != "VERIFIED_PROSPECTIVE_CAPTURE"
                or event["source"] not in {"original", "supplement"}
                or type(event["decisions"]) is not int
                or event["decisions"] != 4
            ):
                return unavailable
            seen.add(event["event"])
            by_asset[asset]["captured"] += 1
            decisions += event["decisions"]
            if event.get("outcome") == "VERIFIED_OFFICIAL_EVALUATION":
                # Missing model probabilities are not scored by the verified collector.
                # Four decisions have at most five model scores each, not always twenty.
                if (
                    type(event.get("score_rows")) is not int
                    or not 0 <= event["score_rows"] <= event["decisions"] * 5
                ):
                    return unavailable
                evaluated += 1
                by_asset[asset]["evaluated"] += 1
        expected = {
            "distinct_captured_events": len(selected),
            "selected_evaluated_events": evaluated,
            "selected_research_decisions": decisions,
        }
        if any(
            type(data.get(key)) is not int or data[key] != value
            for key, value in expected.items()
        ):
            return unavailable
        if (
            not isinstance(data.get("by_asset"), dict)
            or any(type(value) is not int for value in data["by_asset"].values())
            or data["by_asset"] != {asset: counts["captured"] for asset, counts in by_asset.items()}
        ):
            return unavailable
        sources = data["strata"]
        source_times = {name: _clock(sources[name]["as_of"]) for name in ("original", "supplement")}
        if any(value > published for value in source_times.values()):
            return unavailable
        return {
            "available": True,
            **expected,
            "by_asset": by_asset,
            "published_at": published.isoformat(),
            "source_times": {name: value.isoformat() for name, value in source_times.items()},
            "stale": (current - min(source_times.values())).total_seconds() > 4500,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unavailable


def render_progress(report: dict[str, Any]) -> str:
    prefix = "<section id='multiasset-progress'><h2>Prospective multiasset evidence</h2>"
    if not report.get("available"):
        return prefix + "<p>Verified cohort audit unavailable. Counts are unknown.</p></section>"
    status = "Older saved audit" if report["stale"] else "Saved cohort audit"
    rows = "".join(
        f"<tr><td>{asset}</td><td>{report['by_asset'][asset]['captured']}</td>"
        f"<td>{report['by_asset'][asset]['evaluated']}</td></tr>"
        for asset in ASSETS
    )
    return (
        prefix
        + f"<p>{status}: {escape(report['published_at'])}.</p>"
        + f"<p><strong>{report['distinct_captured_events']} captured events</strong> "
        + f"toward the minimum of 20; {report['selected_evaluated_events']} officially evaluated; "
        + f"{report['selected_research_decisions']} durable research decisions.</p>"
        + "<p>Counts reflect completed audit snapshots and may lag scheduled captures. "
        + "Shared assets and hours are not certified independent trials. "
        + "Research evaluations are not paper settlements or qualified full-net-EV trades.</p>"
        + "<table><thead><tr><th>Asset</th><th>Captured</th><th>Evaluated</th></tr></thead>"
        + f"<tbody>{rows}</tbody></table>"
        + f"<p>Original cohort as of {escape(report['source_times']['original'])}; "
        + f"DOGE supplement as of {escape(report['source_times']['supplement'])}. "
        + "Duplicate asset-events are counted once; source model metrics remain separate.</p>"
        + "</section>"
    )
