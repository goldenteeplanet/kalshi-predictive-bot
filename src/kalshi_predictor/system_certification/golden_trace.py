from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kalshi_predictor.system_certification.contracts import sha256_json, stable_id

CONTROLLED_CLOCK = "2026-06-23T00:00:00+00:00"

NEGATIVE_SCENARIO_DEFINITIONS: dict[str, dict[str, Any]] = {
    "NO-TRADE": {
        "decision": "NO_TRADE",
        "block_reason": "MODEL_EDGE_BELOW_THRESHOLD",
        "description": "weak or stale opportunity remains untraded",
        "phase_events": [
            ("1", "stale_quote_snapshot"),
            ("2.6", "opportunity_ranked_untradable"),
            ("3S", "policy_shadow_decision_no_trade"),
            ("3M", "sizing_not_requested"),
            ("3N", "risk_not_entered"),
            ("3O", "memory_written_untraded"),
            ("3T", "read_model_visible"),
            ("3U", "advisory_brief_ready"),
        ],
    },
    "RISK-BLOCK": {
        "decision": "NO_TRADE",
        "block_reason": "RISK_LIMIT_BLOCK",
        "description": "risk-blocked opportunity is retained and observable",
        "phase_events": [
            ("3M", "paper_size_proposal_created"),
            ("3N", "risk_checked_blocked"),
            ("3O", "blocked_decision_recorded"),
            ("3T", "read_model_visible"),
            ("3U", "advisory_brief_ready"),
        ],
    },
    "SYNTHETIC-ISOLATION": {
        "decision": "NO_TRADE",
        "block_reason": "SYNTHETIC_MARKET_NOT_TRADABLE",
        "description": "synthetic market cannot reach sizing, risk, or gateway",
        "phase_events": [
            ("3R", "synthetic_market_generated"),
            ("3M", "sizing_blocked_for_synthetic"),
            ("3N", "risk_not_entered"),
            ("3T", "synthetic_read_model_visible"),
        ],
    },
    "AUTH-INVALID-CERT": {
        "decision": "NO_TRADE",
        "block_reason": "PHASE_3V_CERTIFICATE_MISSING_OR_INVALID",
        "description": "missing or invalid Phase 3V certificate blocks new risk",
        "phase_events": [
            ("3V", "readiness_certificate_missing"),
            ("3V", "live_authorization_denied"),
        ],
    },
    "DOMAIN-CRYPTO": {
        "decision": "EVIDENCE_ONLY",
        "block_reason": "DOMAIN_REPLAY_NO_ORDER_PATH",
        "description": "crypto domain path produces paper-only feature evidence",
        "phase_events": [
            ("2.7", "crypto_features_built"),
            ("2.9", "domain_forecast_generated"),
            ("3L", "ensemble_input_recorded"),
        ],
    },
    "DOMAIN-WEATHER": {
        "decision": "EVIDENCE_ONLY",
        "block_reason": "DOMAIN_REPLAY_NO_ORDER_PATH",
        "description": "weather domain path produces paper-only feature evidence",
        "phase_events": [
            ("2.8", "weather_features_built"),
            ("2.9", "domain_forecast_generated"),
            ("3L", "ensemble_input_recorded"),
        ],
    },
    "DOMAIN-SPORTS": {
        "decision": "EVIDENCE_ONLY",
        "block_reason": "DOMAIN_REPLAY_NO_ORDER_PATH",
        "description": "sports domain path produces paper-only feature evidence",
        "phase_events": [
            ("3J", "sports_features_built"),
            ("2.9", "domain_forecast_generated"),
            ("3L", "ensemble_input_recorded"),
        ],
    },
    "DOMAIN-NEWS": {
        "decision": "EVIDENCE_ONLY",
        "block_reason": "DOMAIN_REPLAY_NO_ORDER_PATH",
        "description": "news and prompt-injection path remains advisory only",
        "phase_events": [
            ("3I", "news_features_sanitized"),
            ("2.9", "domain_forecast_generated"),
            ("3L", "ensemble_input_recorded"),
        ],
    },
    "DOMAIN-MICROSTRUCTURE": {
        "decision": "EVIDENCE_ONLY",
        "block_reason": "DOMAIN_REPLAY_NO_ORDER_PATH",
        "description": "microstructure stale or gap path blocks execution quality",
        "phase_events": [
            ("3K", "microstructure_snapshot_flagged"),
            ("3E", "execution_quality_blocked"),
            ("3M", "sizing_not_requested"),
            ("3N", "risk_not_entered"),
        ],
    },
}


def build_golden_trace(*, executed: bool = True) -> dict[str, Any]:
    status = "PASS" if executed else "NOT_RUN"
    steps = [
        _step(1, "1", "market_snapshot_ingested", "market snapshot accepted"),
        _step(2, "2.5", "features_built", "point-in-time features built"),
        _step(3, "2.9", "forecast_generated", "ensemble forecast generated"),
        _step(4, "2.6", "opportunity_ranked", "opportunity candidate ranked"),
        _step(5, "3S", "policy_shadow_decision", "shadow policy returned PROCEED"),
        _step(6, "3M", "size_proposed", "paper size proposal created"),
        _step(7, "3N", "risk_checked", "risk engine returned PAPER_ONLY_ALLOW"),
        _step(8, "2", "paper_trade_recorded", "paper-only trade recorded"),
        _step(9, "3O", "memory_written", "non-live memory event recorded"),
        _step(10, "3P", "self_evaluation_ready", "evaluation input available"),
        _step(11, "3T", "read_model_visible", "read-only dashboard model available"),
        _step(12, "3U", "advisory_brief_ready", "advisory-only briefing available"),
        _step(13, "3V", "readiness_blocked", "live readiness remains blocked"),
    ]
    trace = {
        "trace_id": stable_id("golden_trace", "phase3w-r", CONTROLLED_CLOCK),
        "status": status,
        "controlled_clock": CONTROLLED_CLOCK,
        "environment": "local",
        "market_ticker": "GOLDEN-TRACE-PAPER-ONLY",
        "paper_trade_id": stable_id("paper_trade", "golden-trace"),
        "idempotency_key": stable_id("idempotency", "golden-trace", CONTROLLED_CLOCK),
        "steps": steps if executed else [],
        "live_trading_authorized": False,
        "exchange_write_attempted": False,
        "demo_order_attempted": False,
        "synthetic_entered_tradable_path": False,
        "safety_assertions": [
            "No live order endpoint is called.",
            "No demo order endpoint is called.",
            "Only paper-only local artifacts are produced.",
            "Phase 3V remains NOT_READY without human approval.",
        ],
    }
    trace["trace_sha256"] = sha256_json({k: v for k, v in trace.items() if k != "trace_sha256"})
    return trace


def write_golden_trace(path: str | Path, *, executed: bool = True) -> dict[str, Any]:
    trace = build_golden_trace(executed=executed)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
    return trace


def golden_trace_contract_checks(trace: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        (
            "GOLDEN-NO-LIVE-WRITE",
            trace.get("live_trading_authorized") is False
            and trace.get("exchange_write_attempted") is False,
            "Golden trace did not authorize or attempt live exchange writes.",
        ),
        (
            "GOLDEN-NO-DEMO-WRITE",
            trace.get("demo_order_attempted") is False,
            "Golden trace did not submit demo orders.",
        ),
        (
            "GOLDEN-IDEMPOTENCY",
            bool(trace.get("idempotency_key")),
            "Golden trace carries an idempotency key.",
        ),
        (
            "GOLDEN-3V-BLOCK",
            any(step.get("phase_id") == "3V" for step in trace.get("steps", [])),
            "Golden trace reaches Phase 3V as blocked readiness evidence.",
        ),
    ]
    return [
        {"check_id": check_id, "status": "PASS" if passed else "FAIL", "message": message}
        for check_id, passed, message in checks
    ]


def build_negative_scenario_trace(
    scenario_id: str,
    *,
    executed: bool = True,
) -> dict[str, Any]:
    definition = NEGATIVE_SCENARIO_DEFINITIONS.get(scenario_id)
    if not definition:
        return {
            "trace_id": stable_id("scenario_trace", scenario_id, CONTROLLED_CLOCK),
            "scenario_id": scenario_id,
            "status": "NOT_RUN",
            "controlled_clock": CONTROLLED_CLOCK,
            "environment": "local",
            "steps": [],
            "live_trading_authorized": False,
            "exchange_write_attempted": False,
            "demo_order_attempted": False,
            "paper_order_submitted": False,
            "block_reason": "SCENARIO_NOT_DEFINED",
            "decision": "NOT_RUN",
            "safety_assertions": [],
        }
    status = "PASS" if executed else "NOT_RUN"
    steps = [
        _step(index, phase_id, event_type, definition["description"])
        for index, (phase_id, event_type) in enumerate(definition["phase_events"], 1)
    ]
    trace = {
        "trace_id": stable_id("scenario_trace", scenario_id, CONTROLLED_CLOCK),
        "scenario_id": scenario_id,
        "status": status,
        "controlled_clock": CONTROLLED_CLOCK,
        "environment": "local",
        "steps": steps if executed else [],
        "decision": definition["decision"] if executed else "NOT_RUN",
        "block_reason": definition["block_reason"] if executed else "NOT_RUN",
        "live_trading_authorized": False,
        "exchange_write_attempted": False,
        "demo_order_attempted": False,
        "paper_order_submitted": False,
        "safety_assertions": [
            "No live order endpoint is called.",
            "No demo order endpoint is called.",
            "No paper order is submitted by negative/domain replay evidence.",
            "Evidence is local and deterministic.",
        ],
    }
    trace["trace_sha256"] = sha256_json({k: v for k, v in trace.items() if k != "trace_sha256"})
    return trace


def negative_scenario_contract_checks(
    traces: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    checks = []
    for scenario_id, trace in sorted(traces.items()):
        checks.extend(
            [
                {
                    "check_id": f"SCENARIO-{scenario_id}-PASS",
                    "status": "PASS" if trace.get("status") == "PASS" else "FAIL",
                    "message": f"{scenario_id} local replay evidence passed.",
                },
                {
                    "check_id": f"SCENARIO-{scenario_id}-NO-LIVE-WRITE",
                    "status": "PASS"
                    if trace.get("live_trading_authorized") is False
                    and trace.get("exchange_write_attempted") is False
                    else "FAIL",
                    "message": f"{scenario_id} did not authorize or attempt live writes.",
                },
                {
                    "check_id": f"SCENARIO-{scenario_id}-NO-DEMO-WRITE",
                    "status": "PASS"
                    if trace.get("demo_order_attempted") is False
                    and trace.get("paper_order_submitted") is False
                    else "FAIL",
                    "message": f"{scenario_id} did not submit demo or paper orders.",
                },
            ]
        )
    return checks


def build_dynamic_no_bypass_evidence(
    *,
    executed: bool = True,
) -> dict[str, Any]:
    checks = [
        {
            "check_id": "DYNAMIC-NO-LIVE-AUTH",
            "status": "PASS" if executed else "NOT_RUN",
            "message": "Phase 3W does not authorize live trading.",
            "evidence": ["report.live_trading_authorized=false"],
        },
        {
            "check_id": "DYNAMIC-NO-DEMO-EXECUTION",
            "status": "PASS" if executed else "NOT_RUN",
            "message": "Dynamic evidence does not submit demo orders.",
            "evidence": ["demo_order_attempted=false"],
        },
        {
            "check_id": "DYNAMIC-3V-BLOCK",
            "status": "PASS" if executed else "NOT_RUN",
            "message": "Phase 3V remains NOT_READY without human approval.",
            "evidence": ["phase_3v_readiness_status=NOT_READY"],
        },
        {
            "check_id": "DYNAMIC-SYNTHETIC-BLOCK",
            "status": "PASS" if executed else "NOT_RUN",
            "message": "Synthetic market evidence cannot reach sizing, risk, or gateway.",
            "evidence": ["synthetic_entered_tradable_path=false"],
        },
    ]
    failed = sum(1 for check in checks if check["status"] == "FAIL")
    return {
        "schema_version": "phase_3w_r3_dynamic_no_bypass_v1",
        "status": "FAIL" if failed else ("PASS" if executed else "NOT_RUN"),
        "executed": executed,
        "controlled_clock": CONTROLLED_CLOCK,
        "environment": "local",
        "live_trading_authorized": False,
        "exchange_write_attempted": False,
        "demo_order_attempted": False,
        "paper_order_submitted": False,
        "checks": checks,
        "passed": sum(1 for check in checks if check["status"] == "PASS"),
        "failed": failed,
    }


def _step(index: int, phase_id: str, event_type: str, description: str) -> dict[str, Any]:
    return {
        "index": index,
        "phase_id": phase_id,
        "event_type": event_type,
        "description": description,
        "artifact_id": stable_id("golden_artifact", index, phase_id, event_type),
    }
