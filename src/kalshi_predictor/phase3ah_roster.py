from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from kalshi_predictor.paper.settlement_reconciliation import PAPER_ONLY_SAFETY
from kalshi_predictor.utils.time import utc_now

PHASE_3AH_ROSTER_VERSION = "phase3ah_roster_v1"
DEFAULT_ROSTER_TEMPLATE_PATH = Path(
    "reports/phase3ah_sports/phase3ah_roster_review_template.json"
)
DEFAULT_OUTPUT_DIR = Path("reports/phase3ah_sports")
VERIFIED_STATUSES = {"APPROVED", "VERIFIED", "READY", "REVIEWED_VERIFIED"}
VALID_ENTITY_TYPES = {"PLAYER", "PARTICIPANT"}


@dataclass(frozen=True)
class Phase3AHRosterArtifactSet:
    output_dir: Path
    json_path: Path
    markdown_path: Path
    verified_evidence_path: Path
    rework_queue_path: Path
    player_prop_blockers_path: Path


def build_phase3ah_roster_verification(
    *,
    roster_template_path: Path = DEFAULT_ROSTER_TEMPLATE_PATH,
    require_source_url: bool = True,
    require_valid_from: bool = True,
    limit: int | None = None,
) -> dict[str, Any]:
    """Validate manually reviewed player/participant roster evidence.

    This phase is report-only. It does not create verified sports links, update team aliases,
    or alter execution settings.
    """

    rows = _load_roster_rows(roster_template_path)
    if limit is not None:
        rows = rows[:limit]

    verified: list[dict[str, Any]] = []
    rework: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    covered_tickers: set[str] = set()
    blocked_tickers: set[str] = set()

    for row in rows:
        validation = _validate_roster_row(
            row,
            require_source_url=require_source_url,
            require_valid_from=require_valid_from,
        )
        if validation["valid"]:
            evidence = _verified_evidence_row(row)
            verified.append(evidence)
            covered_tickers.update(
                str(ticker) for ticker in row.get("example_player_prop_tickers", [])
            )
        else:
            reasons = validation["reasons"]
            for reason in reasons:
                reason_counts[reason] += 1
            blocked_tickers.update(
                str(ticker) for ticker in row.get("example_player_prop_tickers", [])
            )
            rework.append(_rework_row(row, reasons))

    blockers = _player_prop_blockers(rework)
    league_summary = _league_summary(verified, rework)
    blocked_tickers.difference_update(covered_tickers)

    return {
        "generated_at": utc_now().isoformat(),
        "phase": "3AH_ROSTER",
        "phase_version": PHASE_3AH_ROSTER_VERSION,
        "mode": "PAPER_ONLY_ROSTER_PARTICIPANT_VERIFICATION",
        "paper_only_safety": PAPER_ONLY_SAFETY,
        "roster_template_path": str(roster_template_path),
        "auto_upgrade_policy": {
            "phase3ah_roster_creates_verified_links": False,
            "auto_upgrades_created": 0,
            "policy": (
                "Verified roster evidence can unblock later review, but Phase 3AE remains "
                "the only command allowed to create verified sports links."
            ),
        },
        "validation_policy": {
            "verified_statuses": sorted(VERIFIED_STATUSES),
            "require_source_url": require_source_url,
            "require_valid_from": require_valid_from,
            "requires_safe_to_apply": True,
            "requires_canonical_player_id": True,
            "requires_current_team_key": True,
            "requires_current_team_name": True,
        },
        "summary": {
            "roster_rows_reviewed": len(rows),
            "verified_roster_rows": len(verified),
            "rework_rows": len(rework),
            "player_prop_example_tickers_covered": len(covered_tickers),
            "player_prop_example_tickers_still_blocked": len(blocked_tickers),
            "player_prop_blocker_rows": len(blockers),
            "auto_upgrades_created": 0,
        },
        "reason_breakdown": [
            {"reason": reason, "count": count} for reason, count in reason_counts.most_common()
        ],
        "league_summary": league_summary,
        "verified_roster_evidence": verified,
        "rework_queue": rework,
        "player_prop_blockers": blockers,
        "recommended_next_action": _recommended_next_action(verified=verified, rework=rework),
    }


def write_phase3ah_roster_verification_report(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    roster_template_path: Path = DEFAULT_ROSTER_TEMPLATE_PATH,
    require_source_url: bool = True,
    require_valid_from: bool = True,
    limit: int | None = None,
) -> Phase3AHRosterArtifactSet:
    payload = build_phase3ah_roster_verification(
        roster_template_path=roster_template_path,
        require_source_url=require_source_url,
        require_valid_from=require_valid_from,
        limit=limit,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "phase3ah_roster_participant_verification.json"
    markdown_path = output_dir / "phase3ah_roster_participant_verification.md"
    verified_path = output_dir / "phase3ah_verified_roster_evidence.json"
    rework_path = output_dir / "phase3ah_roster_rework_queue.json"
    blockers_path = output_dir / "phase3ah_player_prop_blockers.json"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    verified_path.write_text(
        json.dumps(payload["verified_roster_evidence"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    rework_path.write_text(
        json.dumps(payload["rework_queue"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    blockers_path.write_text(
        json.dumps(payload["player_prop_blockers"], indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return Phase3AHRosterArtifactSet(
        output_dir,
        json_path,
        markdown_path,
        verified_path,
        rework_path,
        blockers_path,
    )


def _load_roster_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing roster review template: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("roster_review_template", payload.get("rows", []))
    else:
        rows = payload
    return [row for row in rows if isinstance(row, dict)]


def _validate_roster_row(
    row: dict[str, Any],
    *,
    require_source_url: bool,
    require_valid_from: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    status = str(row.get("review_status") or "").upper()
    entity_type = str(row.get("verified_entity_type") or "").upper()
    if status not in VERIFIED_STATUSES:
        reasons.append("REVIEW_STATUS_NOT_VERIFIED")
    if row.get("safe_to_apply") is not True:
        reasons.append("SAFE_TO_APPLY_FALSE")
    if entity_type not in VALID_ENTITY_TYPES:
        reasons.append("INVALID_ENTITY_TYPE")
    if not _text(row.get("player_name")):
        reasons.append("MISSING_PLAYER_NAME")
    if not _text(row.get("canonical_player_id")):
        reasons.append("MISSING_CANONICAL_PLAYER_ID")
    if not _text(row.get("current_team_key")):
        reasons.append("MISSING_CURRENT_TEAM_KEY")
    if not _text(row.get("current_team_name")):
        reasons.append("MISSING_CURRENT_TEAM_NAME")
    if require_source_url and not _valid_source(row.get("roster_source_url")):
        reasons.append("MISSING_ROSTER_SOURCE_URL")

    valid_from = _date_or_none(row.get("valid_from"))
    valid_to = _date_or_none(row.get("valid_to")) if _text(row.get("valid_to")) else None
    if require_valid_from and valid_from is None:
        reasons.append("INVALID_VALID_FROM")
    if _text(row.get("valid_to")) and valid_to is None:
        reasons.append("INVALID_VALID_TO")
    if valid_from is not None and valid_to is not None and valid_to < valid_from:
        reasons.append("VALID_TO_BEFORE_VALID_FROM")

    return {"valid": not reasons, "reasons": reasons}


def _verified_evidence_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        "league": _text(row.get("league")),
        "player_name": _text(row.get("player_name")),
        "canonical_player_id": _text(row.get("canonical_player_id")),
        "current_team_key": _text(row.get("current_team_key")),
        "current_team_name": _text(row.get("current_team_name")),
        "roster_source_url": _text(row.get("roster_source_url")),
        "valid_from": _date_text(row.get("valid_from")),
        "valid_to": _date_text(row.get("valid_to")),
        "verified_entity_type": str(row.get("verified_entity_type") or "PLAYER").upper(),
        "review_status": str(row.get("review_status") or "").upper(),
        "example_tickers": list(row.get("example_tickers") or []),
        "example_player_prop_tickers": list(row.get("example_player_prop_tickers") or []),
        "count": int(row.get("count") or 0),
        "safe_to_apply": True,
        "blocks_team_link_upgrade": False,
        "source": "phase3ah_roster_participant_verification",
    }
    evidence["evidence_id"] = _evidence_id(evidence)
    return evidence


def _rework_row(row: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "league": row.get("league"),
        "player_name": row.get("player_name"),
        "count": row.get("count", 0),
        "review_status": row.get("review_status"),
        "safe_to_apply": row.get("safe_to_apply"),
        "current_team_key": row.get("current_team_key"),
        "current_team_name": row.get("current_team_name"),
        "canonical_player_id": row.get("canonical_player_id"),
        "roster_source_url": row.get("roster_source_url"),
        "valid_from": row.get("valid_from"),
        "valid_to": row.get("valid_to"),
        "example_tickers": row.get("example_tickers", []),
        "example_player_prop_tickers": row.get("example_player_prop_tickers", []),
        "rework_reasons": reasons,
        "next_action": _next_action_for_reasons(reasons),
    }


def _player_prop_blockers(rework_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for row in rework_rows:
        tickers = [str(ticker) for ticker in row.get("example_player_prop_tickers", [])]
        if not tickers:
            tickers = [str(ticker) for ticker in row.get("example_tickers", [])]
        blockers.append(
            {
                "league": row.get("league"),
                "player_name": row.get("player_name"),
                "count": row.get("count", 0),
                "blocked_example_tickers": tickers,
                "rework_reasons": row.get("rework_reasons", []),
                "next_action": row.get("next_action"),
            }
        )
    return sorted(
        blockers,
        key=lambda item: (-int(item.get("count") or 0), str(item.get("player_name") or "")),
    )


def _league_summary(
    verified_rows: list[dict[str, Any]],
    rework_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    leagues = sorted(
        {
            str(row.get("league") or "UNKNOWN")
            for row in [*verified_rows, *rework_rows]
        }
    )
    summary: list[dict[str, Any]] = []
    for league in leagues:
        verified = [row for row in verified_rows if str(row.get("league") or "UNKNOWN") == league]
        rework = [row for row in rework_rows if str(row.get("league") or "UNKNOWN") == league]
        reasons: Counter[str] = Counter()
        for row in rework:
            reasons.update(str(reason) for reason in row.get("rework_reasons", []))
        summary.append(
            {
                "league": league,
                "verified_roster_rows": len(verified),
                "rework_rows": len(rework),
                "top_rework_reasons": [
                    {"reason": reason, "count": count}
                    for reason, count in reasons.most_common(5)
                ],
            }
        )
    return summary


def _next_action_for_reasons(reasons: list[str]) -> str:
    if "REVIEW_STATUS_NOT_VERIFIED" in reasons or "SAFE_TO_APPLY_FALSE" in reasons:
        return "Human-review this roster row and mark it verified only with source evidence."
    if "MISSING_ROSTER_SOURCE_URL" in reasons:
        return "Add an official roster or participant source URL before using this row."
    if "MISSING_CURRENT_TEAM_KEY" in reasons or "MISSING_CURRENT_TEAM_NAME" in reasons:
        return "Map the player/participant to one verified current team or participant entry."
    if "MISSING_CANONICAL_PLAYER_ID" in reasons:
        return "Add a stable canonical player or participant id from the source roster."
    if any(
        reason.startswith("INVALID_VALID") or reason.startswith("VALID_TO")
        for reason in reasons
    ):
        return "Add valid roster effective dates before using this evidence."
    return "Complete missing roster evidence before rerunning Phase 3AE."


def _recommended_next_action(
    *,
    verified: list[dict[str, Any]],
    rework: list[dict[str, Any]],
) -> str:
    if rework and not verified:
        return (
            "Complete the roster review template with verified player ids, teams, source "
            "URLs, and valid-from dates before rerunning Phase 3AE."
        )
    if rework:
        return (
            "Some roster evidence is verified, but unresolved player props remain blocked. "
            "Fix the rework queue before rerunning Phase 3AE broadly."
        )
    return "Roster evidence is complete. Rerun Phase 3AE to apply its clean link gate."


def _valid_source(value: object) -> bool:
    text = _text(value)
    return text.startswith(("http://", "https://"))


def _date_or_none(value: object) -> date | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _date_text(value: object) -> str:
    parsed = _date_or_none(value)
    return parsed.isoformat() if parsed is not None else ""


def _text(value: object) -> str:
    return str(value or "").strip()


def _evidence_id(row: dict[str, Any]) -> str:
    key = "|".join(
        str(row.get(field) or "")
        for field in (
            "league",
            "player_name",
            "canonical_player_id",
            "current_team_key",
            "valid_from",
            "valid_to",
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Phase 3AH Roster / Participant Verification",
        "",
        f"- Generated at: {payload['generated_at']}",
        f"- Mode: {payload['mode']}",
        f"- Safety: {payload['paper_only_safety']}",
        f"- Roster template: `{payload['roster_template_path']}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in summary.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Safety Gate",
            "",
            f"- Auto-upgrades created: {payload['auto_upgrade_policy']['auto_upgrades_created']}",
            f"- Policy: {payload['auto_upgrade_policy']['policy']}",
            "",
            "## Rework Reasons",
            "",
            "| Reason | Count |",
            "| --- | ---: |",
        ]
    )
    for row in payload["reason_breakdown"]:
        lines.append(f"| {row['reason']} | {row['count']} |")
    if not payload["reason_breakdown"]:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## League Summary",
            "",
            "| League | Verified | Rework | Top reasons |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for row in payload["league_summary"]:
        reasons = ", ".join(
            f"{item['reason']}={item['count']}" for item in row["top_rework_reasons"]
        )
        lines.append(
            f"| {row['league']} | {row['verified_roster_rows']} | "
            f"{row['rework_rows']} | {reasons or 'none'} |"
        )
    lines.extend(["", "## Top Player Prop Blockers", ""])
    lines.extend(["| League | Player | Count | Reasons |", "| --- | --- | ---: | --- |"])
    for row in payload["player_prop_blockers"][:30]:
        lines.append(
            f"| {row['league']} | {row['player_name']} | {row['count']} | "
            f"{', '.join(row['rework_reasons'])} |"
        )
    if not payload["player_prop_blockers"]:
        lines.append("| none |  | 0 |  |")
    lines.extend(["", "## Recommended Next Action", "", payload["recommended_next_action"], ""])
    return "\n".join(lines)
