from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from kalshi_predictor.data.schema import Base
from kalshi_predictor.utils.time import utc_now

GH2_SOAK = "GH2_OPERATIONAL_SOAK"
CURRENT_RESEARCH = "CURRENT_MARKET_RESEARCH"
HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
GUARDED_PAPER = "GUARDED_PAPER_LEARNING"
LANE_CONTRACT_VERSION = "canonical_system_lanes_v1"

LANES = {
    GH2_SOAK: {
        "purpose": "Prove bounded operational health and safety.",
        "writes": "Health, stage, source-readiness, soak, and invariant evidence only.",
        "prohibitions": ["research expansion", "paper order creation", "shared training counters"],
    },
    CURRENT_RESEARCH: {
        "purpose": "Scan current markets and produce forecasts, rankings, and shadow evidence.",
        "writes": (
            "Current catalog, source, link, feature, quote, forecast, ranking, and shadow data."
        ),
        "prohibitions": ["paper orders", "historical-label leakage", "GH-2 soak counters"],
    },
    HISTORICAL_REPLAY: {
        "purpose": "Produce no-lookahead historical and settled model evaluations.",
        "writes": "Replay, backtest, settlement, calibration, memory, and evaluation evidence.",
        "prohibitions": ["current paper orders", "post-outcome features", "GH-2 soak counters"],
    },
    GUARDED_PAPER: {
        "purpose": "Create and evaluate explicitly authorized simulated paper positions.",
        "writes": "Paper orders, fills, positions, P&L, sizing, risk, and paper-learning evidence.",
        "prohibitions": ["live execution", "unapproved paper creation", "shadow-as-paper"],
    },
}


@dataclass(frozen=True)
class OwnedResource:
    kind: str
    name: str
    lane: str
    mode: str
    definition: str


_PAPER_TABLES = {
    "paper_orders",
    "paper_fills",
    "paper_positions",
    "paper_pnl",
    "position_sizing_decisions",
    "advanced_risk_decisions",
    "advanced_risk_reservations",
    "advanced_risk_high_water_marks",
    "risk_events",
    "position_history",
    "portfolio_snapshots",
    "learning_runs",
    "learning_cycles",
    "learning_rejection_log",
    "learning_trade_targets",
    "learning_opportunities",
    "learning_paper_trades",
    "learning_metrics",
    "autopilot_runs",
    "autopilot_cycles",
    "autopilot_opportunities",
    "autopilot_paper_trades",
    "autopilot_metrics",
    "overnight_runs",
    "overnight_cycles",
}

_REPLAY_PREFIXES = (
    "backtest_",
    "model_tournament_",
    "model_iteration_",
    "model_confidence_",
    "model_leaderboard",
    "model_weights",
    "model_diagnostics",
    "market_memory",
    "forecast_memory",
    "trade_memory",
    "memory_",
    "self_evaluation_",
    "feature_discovery_",
    "feature_candidate",
    "feature_evaluation",
    "feature_fold_",
    "feature_segment_",
    "feature_relationship",
    "feature_recommendation",
    "feature_holdout_",
    "synthetic_",
    "rl_",
    "meta_model_training_",
    "meta_model_performance",
)

_SOAK_TABLES = {
    "runtime_provenance_events",
    "readiness_review",
    "readiness_control_result",
    "readiness_evidence_manifest",
    "readiness_decision",
    "live_readiness_certificate",
    "live_readiness_certificate_event",
    "system_certification_run",
    "system_certification_artifact",
}

ARTIFACTS = (
    OwnedResource(
        "artifact",
        "reports/fixed_rate_health_status.json",
        GH2_SOAK,
        "WRITE",
        "One scheduler-cycle health state.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase_gh2/gh2_active_candidate_refresh.json",
        GH2_SOAK,
        "WRITE",
        "Bounded GH-2 decision evidence.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase3bc_r5/phase3bc_r5_status.json",
        GH2_SOAK,
        "WRITE",
        "Operational freshness-watch status.",
    ),
    OwnedResource(
        "artifact",
        "reports/paper_activation/invariant_status.json",
        GH2_SOAK,
        "WRITE",
        "Read-only invariant assessment for authorized paper orders.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase3bc_r3/phase3bc_r3_active_crypto_refresh.json",
        CURRENT_RESEARCH,
        "WRITE",
        "Current crypto refresh universe and forecast evidence.",
    ),
    OwnedResource(
        "artifact",
        "reports/crypto_event_vectors/status.json",
        CURRENT_RESEARCH,
        "WRITE",
        "Coherent current-event quote captures.",
    ),
    OwnedResource(
        "artifact",
        "reports/crypto_event_vectors/forecast_polytope_alignment.json",
        CURRENT_RESEARCH,
        "WRITE",
        "Current forecast-to-quote-polytope alignment.",
    ),
    OwnedResource(
        "artifact",
        "reports/crypto_event_vectors/targeted_forecast_telemetry.json",
        CURRENT_RESEARCH,
        "WRITE",
        "Targeted current-forecast yield telemetry.",
    ),
    OwnedResource(
        "artifact",
        "reports/overnight_alpha_factory/shadow_signals.jsonl",
        CURRENT_RESEARCH,
        "WRITE",
        "Current shadow signals; never paper orders.",
    ),
    OwnedResource(
        "artifact",
        "reports/overnight_alpha_factory/shadow_trades.jsonl",
        CURRENT_RESEARCH,
        "WRITE",
        "Current simulated shadow ledger.",
    ),
    OwnedResource(
        "artifact",
        "reports/crypto_event_vectors/multiclass_interval_scoring.json",
        HISTORICAL_REPLAY,
        "WRITE",
        "Settled no-lookahead multiclass evaluation.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase3aa",
        HISTORICAL_REPLAY,
        "WRITE",
        "Settlement realization and reconciliation evidence.",
    ),
    OwnedResource(
        "artifact",
        "reports/paper_settlement_reconciliation",
        HISTORICAL_REPLAY,
        "WRITE",
        "Canonical matching diagnostics; paper lane is read-only consumer.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase3ba_r3/weather_paper_gate.json",
        GUARDED_PAPER,
        "WRITE",
        "Paper eligibility gate; not authorization.",
    ),
    OwnedResource(
        "artifact",
        "reports/phase3ba_r3/scoped_weather_depth_preflight.json",
        GUARDED_PAPER,
        "WRITE",
        "Fresh sizing/risk preflight evidence.",
    ),
    OwnedResource(
        "artifact",
        "reports/paper_activation/governance_preflight.json",
        GUARDED_PAPER,
        "WRITE",
        "Operator approval and guarded activation preflight evidence.",
    ),
)

WRITERS = (
    OwnedResource(
        "writer",
        "fixed-rate:health-and-soak-stages",
        GH2_SOAK,
        "WRITE",
        "Health artifact writer under the systemd controller.",
    ),
    OwnedResource(
        "writer",
        "fixed-rate:active-crypto-and-weather-refresh",
        CURRENT_RESEARCH,
        "WRITE",
        "Current catalog/source/feature/forecast/ranking stages.",
    ),
    OwnedResource(
        "writer",
        "crypto-event-quote-collector",
        CURRENT_RESEARCH,
        "WRITE",
        "Current coherent capture and telemetry writer.",
    ),
    OwnedResource(
        "writer",
        "shadow-signal-and-ledger-scripts",
        CURRENT_RESEARCH,
        "WRITE",
        "Artifact-only shadow research writer.",
    ),
    OwnedResource(
        "writer",
        "settlement-refresh-and-replay",
        HISTORICAL_REPLAY,
        "WRITE",
        "Canonical external outcomes and evaluated-history writer.",
    ),
    OwnedResource(
        "writer",
        "backtest-and-walk-forward-commands",
        HISTORICAL_REPLAY,
        "WRITE",
        "No-lookahead replay/evaluation writer.",
    ),
    OwnedResource(
        "writer",
        "operator-approved-paper-activation",
        GUARDED_PAPER,
        "WRITE",
        "Only authorized paper-order creation path.",
    ),
    OwnedResource(
        "writer",
        "phase3m-phase3n-paper-transaction",
        GUARDED_PAPER,
        "WRITE",
        "Sizing and risk records for guarded paper decisions.",
    ),
)

COUNTERS = (
    OwnedResource(
        "counter",
        "gh2_healthy_cycles",
        GH2_SOAK,
        "COUNT",
        "Completed healthy fixed-rate cycles; research runs never increment it.",
    ),
    OwnedResource(
        "counter",
        "gh2_critical_stage_timeouts",
        GH2_SOAK,
        "COUNT",
        "Critical timeouts per operational cycle.",
    ),
    OwnedResource(
        "counter",
        "current_supported_markets",
        CURRENT_RESEARCH,
        "COUNT",
        "Distinct active semantically supported tickers.",
    ),
    OwnedResource(
        "counter",
        "current_forecasted_markets",
        CURRENT_RESEARCH,
        "COUNT",
        "Distinct current tickers with point-in-time forecasts.",
    ),
    OwnedResource(
        "counter",
        "current_positive_net_ev",
        CURRENT_RESEARCH,
        "COUNT",
        "Current fee-adjusted executable candidates with net EV above zero.",
    ),
    OwnedResource(
        "counter",
        "shadow_decisions",
        CURRENT_RESEARCH,
        "COUNT",
        "Artifact-ledger shadow decisions; excludes paper orders.",
    ),
    OwnedResource(
        "counter",
        "replay_evaluated_contracts",
        HISTORICAL_REPLAY,
        "COUNT",
        "No-lookahead evaluated contract forecasts.",
    ),
    OwnedResource(
        "counter",
        "replay_independent_events",
        HISTORICAL_REPLAY,
        "COUNT",
        "Distinct terminal events weighted once.",
    ),
    OwnedResource(
        "counter",
        "settled_shadow_decisions",
        HISTORICAL_REPLAY,
        "COUNT",
        "Shadow decisions joined to canonical settlements.",
    ),
    OwnedResource(
        "counter",
        "guarded_paper_orders",
        GUARDED_PAPER,
        "COUNT",
        "Rows in paper_orders; excludes shadow trades.",
    ),
    OwnedResource(
        "counter",
        "guarded_paper_fills",
        GUARDED_PAPER,
        "COUNT",
        "Rows in paper_fills attributable to guarded paper orders.",
    ),
    OwnedResource(
        "counter",
        "settled_paper_trades",
        GUARDED_PAPER,
        "COUNT",
        "Filled paper orders with attributable canonical settlement.",
    ),
    OwnedResource(
        "counter",
        "evaluated_paper_trades",
        GUARDED_PAPER,
        "COUNT",
        "Settled paper trades with realized model/P&L evaluation.",
    ),
)


def table_owner(table: str) -> str:
    if table in _SOAK_TABLES:
        return GH2_SOAK
    if table in _PAPER_TABLES:
        return GUARDED_PAPER
    if table == "settlements" or table.startswith(_REPLAY_PREFIXES):
        return HISTORICAL_REPLAY
    return CURRENT_RESEARCH


def command_owner(command: str) -> str:
    lowered = command.lower()
    if any(
        token in lowered
        for token in ("paper", "position-sizing", "advanced-risk", "learning", "autopilot", "gh4")
    ):
        return GUARDED_PAPER
    if any(
        token in lowered
        for token in (
            "backtest",
            "replay",
            "walk-forward",
            "calibration",
            "tournament",
            "model-diagnostic",
            "model-weight",
            "settlement",
            "phase3aa",
            "feature-discovery",
            "feature-experiment",
            "memory",
            "self-evaluate",
            "synthetic-market",
            "rl-",
        )
    ):
        return HISTORICAL_REPLAY
    if any(
        token in lowered
        for token in (
            "gh2",
            "fixed-rate",
            "runtime-health",
            "soak",
            "system-certification",
            "live-readiness",
        )
    ):
        return GH2_SOAK
    return CURRENT_RESEARCH


def build_lane_contract(cli_path: Path | None = None) -> dict[str, Any]:
    commands = _registered_commands(cli_path)
    resources = [
        *(
            OwnedResource(
                "table", table, table_owner(table), "WRITE", "Primary durable-table owner."
            )
            for table in sorted(Base.metadata.tables)
        ),
        *(
            OwnedResource(
                "command", command, command_owner(command), "EXECUTE", "Primary command lane."
            )
            for command in commands
        ),
        *ARTIFACTS,
        *WRITERS,
        *COUNTERS,
    ]
    collisions = _collisions(resources)
    lane_counts = {
        lane: {
            kind: sum(1 for item in resources if item.lane == lane and item.kind == kind)
            for kind in ("command", "table", "artifact", "writer", "counter")
        }
        for lane in LANES
    }
    return {
        "version": LANE_CONTRACT_VERSION,
        "generated_at": utc_now().isoformat(),
        "status": "READY" if not collisions else "OWNERSHIP_COLLISION",
        "lanes": LANES,
        "lane_counts": lane_counts,
        "resources": [asdict(item) for item in resources],
        "collisions": collisions,
        "shared_read_contracts": [
            "All lanes read the same semantic market definitions.",
            "All lanes read the same model, feature, EV, and fee definitions.",
            "Historical Replay owns canonical settlements; Guarded Paper consumes them read-only.",
            (
                "GH-2 consumes current-market evidence but never increments research or paper "
                "counters."
            ),
        ],
        "known_physical_boundaries": [
            {
                "resource": "forecasts",
                "owner": CURRENT_RESEARCH,
                "rule": (
                    "Historical reconstruction writes backtest/replay evidence, not current "
                    "forecasts."
                ),
            },
            {
                "resource": "settlements",
                "owner": HISTORICAL_REPLAY,
                "rule": "Paper P&L may read outcomes but may not fabricate or rewrite them.",
            },
            {
                "resource": "fixed-rate-service",
                "owner": GH2_SOAK,
                "rule": "It is a controller; each child writer is charged to its declared lane.",
            },
        ],
    }


def render_lane_contract_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Canonical System Lanes",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Status: `{payload['status']}`",
        "",
    ]
    for lane, metadata in payload["lanes"].items():
        counts = payload["lane_counts"][lane]
        lines.extend(
            [
                f"## {lane}",
                "",
                metadata["purpose"],
                "",
                f"Writes: {metadata['writes']}",
                "",
                "Owned resources: " + ", ".join(f"{key}={value}" for key, value in counts.items()),
                "",
                "Prohibitions: " + ", ".join(metadata["prohibitions"]),
                "",
            ]
        )
        for kind in ("writer", "artifact", "counter"):
            owned = [
                item
                for item in payload["resources"]
                if item["lane"] == lane and item["kind"] == kind
            ]
            lines.append(f"### {kind.title()}s")
            lines.append("")
            lines.extend(f"- `{item['name']}` — {item['definition']}" for item in owned)
            lines.append("")
    lines.extend(["## Table ownership", ""])
    for lane in LANES:
        names = [
            item["name"]
            for item in payload["resources"]
            if item["lane"] == lane and item["kind"] == "table"
        ]
        lines.append(f"- **{lane}:** " + ", ".join(f"`{name}`" for name in names))
    lines.extend(["", "## Command ownership", ""])
    for lane in LANES:
        names = [
            item["name"]
            for item in payload["resources"]
            if item["lane"] == lane and item["kind"] == "command"
        ]
        lines.append(f"- **{lane}:** " + ", ".join(f"`{name}`" for name in names))
    lines.extend(["", "## Shared read contracts", ""])
    lines.extend(f"- {item}" for item in payload["shared_read_contracts"])
    lines.extend(["", "## Physical-boundary findings", ""])
    lines.extend(
        f"- `{item['resource']}` → **{item['owner']}**: {item['rule']}"
        for item in payload["known_physical_boundaries"]
    )
    lines.extend(
        [
            "",
            "## Consolidation gate",
            "",
            (
                "No wrapper may be deleted or redirected until its command and every "
                "artifact/table it writes resolve to the same lane in this contract. "
                "Shadow artifacts remain distinct from guarded paper tables."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_lane_contract(
    *,
    json_path: Path,
    markdown_path: Path,
    cli_path: Path | None = None,
) -> dict[str, Any]:
    payload = build_lane_contract(cli_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_lane_contract_markdown(payload), encoding="utf-8")
    return payload


def _registered_commands(cli_path: Path | None) -> list[str]:
    path = cli_path or Path(__file__).with_name("cli.py")
    source = path.read_text(encoding="utf-8")
    return sorted(set(re.findall(r'@app\.command\(\s*["\']([^"\']+)["\']', source)))


def _collisions(resources: list[OwnedResource]) -> list[dict[str, Any]]:
    owners: dict[tuple[str, str], set[str]] = {}
    for item in resources:
        owners.setdefault((item.kind, item.name), set()).add(item.lane)
    collisions = [
        {"kind": kind, "name": name, "lanes": sorted(lanes)}
        for (kind, name), lanes in sorted(owners.items())
        if len(lanes) > 1
    ]
    artifacts = [item for item in resources if item.kind == "artifact"]
    for index, left in enumerate(artifacts):
        left_parts = PurePosixPath(left.name).parts
        for right in artifacts[index + 1 :]:
            if left.lane == right.lane:
                continue
            right_parts = PurePosixPath(right.name).parts
            shared_prefix = (
                left_parts == right_parts[: len(left_parts)]
                or right_parts == left_parts[: len(right_parts)]
            )
            if shared_prefix:
                collisions.append(
                    {
                        "kind": "artifact_path_overlap",
                        "name": f"{left.name} <> {right.name}",
                        "lanes": sorted({left.lane, right.lane}),
                    }
                )
    return collisions
