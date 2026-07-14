# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.system_certification.connection_registry import legacy_connections
from kalshi_predictor.system_certification.phase_registry import legacy_phases
from kalshi_predictor.utils.decimals import decimal_to_str

SCHEMA_VERSION = "1.1.0"
REGISTRY_VERSION = "phase_3w_r_authoritative_phase_registry_v1"
CONNECTION_REGISTRY_VERSION = "phase_3w_r_typed_connection_registry_v1"
REPORT_VERSION = "phase_3w_r_system_certification_report_v2"

MODE_AUDIT_ONLY = "AUDIT_ONLY"
MODE_LOCAL_INTEGRATION = "LOCAL_INTEGRATION"
MODE_STAGING_READ_ONLY = "STAGING_READ_ONLY"
MODE_SAFE_REPAIR = "SAFE_REPAIR"
MODES = {MODE_AUDIT_ONLY, MODE_LOCAL_INTEGRATION, MODE_STAGING_READ_ONLY, MODE_SAFE_REPAIR}

SYSTEM_PASS = "SYSTEM_PASS"
SYSTEM_CONDITIONAL_PASS = "SYSTEM_CONDITIONAL_PASS"
SYSTEM_FAIL = "SYSTEM_FAIL"
SYSTEM_INCOMPLETE = "SYSTEM_INCOMPLETE"

STATUS_PASS = "PASS"
STATUS_DEGRADED = "DEGRADED"
STATUS_FAIL = "FAIL"
STATUS_INCOMPLETE = "INCOMPLETE"
STATUS_NOT_RUN = "NOT_RUN"
STATUS_NOT_OBSERVED = "NOT_OBSERVED"
STATUS_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
STATUS_MAPPING_ERROR = "MAPPING_ERROR"
STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"
LIVE_AUTH_NOT_AUTHORIZED = "NOT_AUTHORIZED"
PHASE_3V_NOT_READY = "NOT_READY"

E0_CLAIM = "E0_CLAIM"
E1_STATIC = "E1_STATIC"
E2_TEST = "E2_TEST"
E3_REPLAY = "E3_REPLAY"
E4_STAGING_RUNTIME = "E4_STAGING_RUNTIME"
E5_PRODUCTION_READ_ONLY = "E5_PRODUCTION_READ_ONLY"
E6_HUMAN_APPROVAL = "E6_HUMAN_APPROVAL"


@dataclass(frozen=True)
class CertificationConfig:
    enabled: bool
    mode: str
    safe_repair_enabled: bool
    output_dir: str

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(
                "PHASE_3W_MODE must be AUDIT_ONLY, LOCAL_INTEGRATION, "
                "STAGING_READ_ONLY, or SAFE_REPAIR."
            )
        if self.mode == MODE_SAFE_REPAIR and not self.safe_repair_enabled:
            raise ValueError("SAFE_REPAIR mode requires PHASE_3W_SAFE_REPAIR_ENABLED=true.")


PHASES: tuple[dict[str, Any], ...] = (
    {
        "phase_id": "1",
        "name": "Data ingestion, snapshots, settlements",
        "locations": ["src/kalshi_predictor/ingest", "src/kalshi_predictor/kalshi"],
        "feature_flags": ["KALSHI_ENV", "KALSHI_BASE_URL"],
        "inputs": ["Kalshi public market data"],
        "outputs": ["markets", "market_snapshots", "settlements"],
    },
    {
        "phase_id": "2",
        "name": "Paper trading ledger",
        "locations": ["src/kalshi_predictor/paper"],
        "feature_flags": ["PAPER_MIN_EDGE", "PAPER_MAX_ORDER_QUANTITY"],
        "inputs": ["rankings", "market snapshots"],
        "outputs": ["paper_orders", "paper_fills", "paper_positions"],
    },
    {
        "phase_id": "2.5",
        "name": "Backtesting and feature store",
        "locations": ["src/kalshi_predictor/backtesting", "src/kalshi_predictor/features"],
        "feature_flags": [],
        "inputs": ["historical snapshots", "features"],
        "outputs": ["backtest runs", "feature vectors"],
    },
    {
        "phase_id": "2.6",
        "name": "Opportunity scanner and leaderboard",
        "locations": ["src/kalshi_predictor/opportunities", "src/kalshi_predictor/leaderboard"],
        "feature_flags": [],
        "inputs": ["forecasts", "snapshots"],
        "outputs": ["market_rankings", "opportunity reports"],
    },
    {
        "phase_id": "2.7",
        "name": "Crypto v2",
        "locations": ["src/kalshi_predictor/crypto", "src/kalshi_predictor/external/crypto.py"],
        "feature_flags": [],
        "inputs": ["crypto feeds", "crypto market links"],
        "outputs": ["crypto features", "crypto forecasts"],
    },
    {
        "phase_id": "2.8",
        "name": "Weather v2",
        "locations": ["src/kalshi_predictor/weather", "src/kalshi_predictor/external/weather.py"],
        "feature_flags": [],
        "inputs": ["weather feeds", "weather market links"],
        "outputs": ["weather features", "weather forecasts"],
    },
    {
        "phase_id": "2.9",
        "name": "Tournament and ensemble v2",
        "locations": ["src/kalshi_predictor/tournament", "src/kalshi_predictor/forecasting/ensemble_v2.py"],
        "feature_flags": [],
        "inputs": ["domain forecasts", "historical outcomes"],
        "outputs": ["model weights", "ensemble forecasts"],
    },
    {
        "phase_id": "3A",
        "name": "Demo execution and decision UI",
        "locations": ["src/kalshi_predictor/ui", "src/kalshi_predictor/ui/service.py"],
        "feature_flags": ["EXECUTION_ENABLED", "EXECUTION_DRY_RUN", "UI_READ_ONLY"],
        "inputs": ["opportunity detail", "risk checks"],
        "outputs": ["UI actions", "execution reports"],
    },
    {
        "phase_id": "3B",
        "name": "Autopilot and guardrails",
        "locations": ["src/kalshi_predictor/autopilot"],
        "feature_flags": ["AUTOPILOT_ENABLED", "AUTOPILOT_DRY_RUN"],
        "inputs": ["rankings", "guardrails"],
        "outputs": ["autopilot runs", "risk events"],
    },
    {
        "phase_id": "3C",
        "name": "Human-readable UI",
        "locations": ["src/kalshi_predictor/ui/templates", "src/kalshi_predictor/explain"],
        "feature_flags": ["UI_READ_ONLY"],
        "inputs": ["stored decisions", "explanations"],
        "outputs": ["operator views"],
    },
    {
        "phase_id": "3D",
        "name": "Trader workstation",
        "locations": ["src/kalshi_predictor/workstation"],
        "feature_flags": [],
        "inputs": ["portfolio", "alerts", "watchlists"],
        "outputs": ["workstation reports"],
    },
    {
        "phase_id": "3E",
        "name": "Opportunity intelligence and best payouts",
        "locations": ["src/kalshi_predictor/opportunities/payout_scoring.py"],
        "feature_flags": [],
        "inputs": ["rankings", "costs"],
        "outputs": ["best payouts", "net opportunity metrics"],
    },
    {
        "phase_id": "3F",
        "name": "Learning Mode and model confidence",
        "locations": ["src/kalshi_predictor/learning", "src/kalshi_predictor/confidence"],
        "feature_flags": ["LEARNING_MODE"],
        "inputs": ["paper trades", "settled outcomes"],
        "outputs": ["learning cycles", "confidence rows"],
    },
    {
        "phase_id": "3G",
        "name": "PostgreSQL and database hardening",
        "locations": ["src/kalshi_predictor/data"],
        "feature_flags": ["DB_BACKEND", "DATABASE_URL"],
        "inputs": ["all durable writes"],
        "outputs": ["migrations", "maintenance reports"],
    },
    {
        "phase_id": "3H",
        "name": "Quick-settlement hunter",
        "locations": ["src/kalshi_predictor/learning/targets.py", "src/kalshi_predictor/tonight"],
        "feature_flags": ["LEARNING_PRIORITIZE_FAST_SETTLEMENT"],
        "inputs": ["close and settlement timing"],
        "outputs": ["fast-settlement learning targets"],
    },
    {
        "phase_id": "3I",
        "name": "News intelligence",
        "locations": ["src/kalshi_predictor/news"],
        "feature_flags": [],
        "inputs": ["news_items", "market links"],
        "outputs": ["news features", "news forecasts"],
    },
    {
        "phase_id": "3J",
        "name": "Sports intelligence",
        "locations": ["src/kalshi_predictor/sports"],
        "feature_flags": [],
        "inputs": ["sports schedules", "sports market links"],
        "outputs": ["sports features", "sports forecasts"],
    },
    {
        "phase_id": "3K",
        "name": "Market microstructure",
        "locations": ["src/kalshi_predictor/microstructure"],
        "feature_flags": [],
        "inputs": ["order books", "market snapshots"],
        "outputs": ["microstructure features", "liquidity quality"],
    },
    {
        "phase_id": "3L",
        "name": "Meta model",
        "locations": ["src/kalshi_predictor/meta"],
        "feature_flags": [],
        "inputs": ["domain forecasts", "meta features"],
        "outputs": ["meta decisions", "meta reports"],
    },
    {
        "phase_id": "3M",
        "name": "Dynamic position sizing",
        "locations": ["src/kalshi_predictor/position_sizing"],
        "feature_flags": ["DYNAMIC_POSITION_SIZING_MODE"],
        "inputs": ["opportunity", "confidence", "liquidity"],
        "outputs": ["position sizing decisions"],
    },
    {
        "phase_id": "3N",
        "name": "Advanced risk engine",
        "locations": ["src/kalshi_predictor/advanced_risk"],
        "feature_flags": ["ADVANCED_RISK_ENGINE_MODE"],
        "inputs": ["3M proposal", "portfolio state"],
        "outputs": ["ALLOW/REDUCE/BLOCK", "risk reservations"],
    },
    {
        "phase_id": "3O",
        "name": "Market Memory",
        "locations": ["src/kalshi_predictor/memory"],
        "feature_flags": ["PHASE_3O_MARKET_MEMORY_ENABLED"],
        "inputs": ["market", "forecast", "trade", "settlement events"],
        "outputs": ["forecast memory", "trade memory", "archives"],
    },
    {
        "phase_id": "3P",
        "name": "Self-Evaluation Engine",
        "locations": ["src/kalshi_predictor/self_evaluation"],
        "feature_flags": ["PHASE_3P_SELF_EVALUATION_ENABLED"],
        "inputs": ["memory", "settled outcomes"],
        "outputs": ["self-evaluation journals"],
    },
    {
        "phase_id": "3Q",
        "name": "Auto Feature Discovery",
        "locations": ["src/kalshi_predictor/feature_discovery"],
        "feature_flags": ["PHASE_3Q_FEATURE_DISCOVERY_ENABLED"],
        "inputs": ["memory datasets"],
        "outputs": ["feature experiments", "scorecards"],
    },
    {
        "phase_id": "3R",
        "name": "Synthetic Markets",
        "locations": ["src/kalshi_predictor/synthetic_markets"],
        "feature_flags": ["PHASE_3R_SYNTHETIC_MARKETS_ENABLED"],
        "inputs": ["synthetic candidates"],
        "outputs": ["non-tradable synthetic reports"],
    },
    {
        "phase_id": "3S",
        "name": "Reinforcement Learning Layer",
        "locations": ["src/kalshi_predictor/reinforcement_learning"],
        "feature_flags": ["PHASE_3S_REINFORCEMENT_LEARNING_ENABLED"],
        "inputs": ["behavior decisions", "rewards"],
        "outputs": ["SKIP/PROCEED policy decisions"],
    },
    {
        "phase_id": "3T",
        "name": "Institutional Dashboard",
        "locations": ["src/kalshi_predictor/institutional_dashboard"],
        "feature_flags": ["PHASE_3T_INSTITUTIONAL_DASHBOARD_ENABLED"],
        "inputs": ["typed read models"],
        "outputs": ["institutional snapshots"],
    },
    {
        "phase_id": "3U",
        "name": "Personal AI Trader",
        "locations": ["src/kalshi_predictor/personal_trader"],
        "feature_flags": ["PHASE_3U_PERSONAL_AI_TRADER_ENABLED"],
        "inputs": ["opportunities", "3M/3N", "portfolio scope"],
        "outputs": ["advisory-only ranked brief"],
    },
    {
        "phase_id": "3V",
        "name": "Live Trading Readiness Review",
        "locations": ["src/kalshi_predictor/live_readiness"],
        "feature_flags": ["PHASE_3V_LIVE_READINESS_ENABLED"],
        "inputs": ["evidence manifest", "control catalog"],
        "outputs": ["readiness decision", "certificate guard result"],
    },
)

CONNECTIONS: tuple[dict[str, Any], ...] = (
    ("E001", "1", "2", "table", "Canonical quote/market/settlement semantics"),
    ("E002", "1", "2.5", "table", "Immutable snapshots and availability times"),
    ("E003", "1", "2.6", "table", "Eligibility, lifecycle, executable quote, freshness"),
    ("E004", "1", "2.7", "table", "Crypto market mapping and cutoff"),
    ("E005", "1", "2.8", "table", "Weather location/rule mapping"),
    ("E006", "1", "3H", "table", "Close and settlement timing"),
    ("E007", "1", "3I", "table", "Market/entity mapping"),
    ("E008", "1", "3J", "table", "Sports event/market mapping"),
    ("E009", "1", "3K", "table", "Synchronized book and trade feed"),
    ("E010", "1", "3O", "table", "Market/snapshot/settlement lineage"),
    ("E011", "2", "3O", "table", "Paper intent/order/fill/outcome lineage"),
    ("E012", "2", "3P", "table", "Settled realized results"),
    ("E013", "2", "3Q", "table", "Full decision universe and net outcomes"),
    ("E014", "2", "3S", "table", "Behavior decisions, propensities/support, rewards"),
    ("E015", "2.5", "2.7", "function", "Point-in-time crypto features"),
    ("E016", "2.5", "2.8", "function", "Point-in-time weather features"),
    ("E017", "2.5", "2.9", "function", "Frozen evaluation dataset"),
    ("E018", "2.5", "3F", "table", "Closed-outcome calibration data"),
    ("E019", "2.5", "3L", "function", "Online/offline-compatible features"),
    ("E020", "2.5", "3Q", "table", "Eligible feature universe"),
    ("E021", "2.5", "3S", "table", "Context/reward evaluation data"),
    ("E022", "2.7", "3L", "function", "Crypto prediction contract"),
    ("E023", "2.8", "3L", "function", "Weather prediction contract"),
    ("E024", "3I", "3L", "function", "News feature contract and trust boundary"),
    ("E025", "3J", "3L", "function", "Sports feature/prediction contract"),
    ("E026", "3K", "3M", "function", "Liquidity, spread, slippage, quality"),
    ("E027", "2.9", "3L", "function", "Ensemble version/member contribution"),
    ("E028", "3F", "3M", "table", "Calibrated confidence contract"),
    ("E029", "3L", "2.6", "table", "Final probability and uncertainty"),
    ("E030", "2.6", "3E", "function", "Candidate universe and executable economics"),
    ("E031", "3H", "3S", "function", "Specialized candidate conforms to normal contract"),
    ("E032", "3E", "3S", "function", "Net opportunity state"),
    ("E033", "3S", "3M", "function", "Only PROCEED reaches sizing"),
    ("E034", "3M", "3N", "function", "Immutable size proposal and reasons"),
    ("E035", "3N", "3A", "function", "Approved demo intent only"),
    ("E036", "3N", "3B", "function", "Approved guarded intent and reservation"),
    ("E037", "3N", "3U", "function", "Accurate risk result and approved size"),
    ("E038", "3B", "3V", "function", "Live intent scope and build fingerprint"),
    ("E039", "3V", "GATEWAY", "function", "Valid current certificate and launch envelope"),
    ("E040", "2", "3O", "table", "Intent/order/fill/position lineage"),
    ("E041", "1", "2", "table", "Idempotent settlement and payout"),
    ("E042", "2", "3O", "table", "Final outcome and P&L"),
    ("E043", "3O", "3P", "table", "Complete nightly evaluation dataset"),
    ("E044", "3O", "3Q", "table", "Point-in-time feature/outcome dataset"),
    ("E045", "3O", "3S", "table", "Historical contexts/actions/rewards"),
    ("E046", "3O", "3T", "table", "Typed read models, freshness, trace IDs"),
    ("E047", "3O", "3U", "table", "Current scope and historical context"),
    ("E048", "3R", "3O", "table", "Synthetic memory with non-tradable flag"),
    ("E049", "3R", "3T", "table", "Clearly synthetic read contract"),
    ("E050", "3R", "3M", "negative_assertion", "Synthetic cannot reach sizing/risk/gateway"),
    ("E051", "3P", "3T", "table", "Journal read model only"),
    ("E052", "3Q", "3T", "table", "Candidate only, no auto promotion"),
    ("E053", "3S", "3T", "table", "Policy artifact only, shadow first"),
    ("E054", "3V", "3V", "manifest", "Phase 3W handoff manifest and explicit limitations"),
    ("E055", "3T", "3N", "negative_assertion", "Read or authorized request contracts only"),
    ("E056", "3G", "OBSERVABILITY", "multiple", "Transactions, constraints, idempotency, restore"),
    ("E057", "3T", "OBSERVABILITY", "multiple", "Correlation/causation and freshness signals"),
)
CONNECTIONS = tuple(
    {
        "connection_id": edge_id,
        "source_phase": source,
        "destination_phase": destination,
        "transport": transport,
        "contract": contract,
    }
    for edge_id, source, destination, transport, contract in CONNECTIONS
)

# Backward-compatible exports. Phase 3W-R keeps the authoritative registries in
# phase_registry.py and connection_registry.py, then renders this older shape for
# callers/tests that predate the remediation.
PHASES = legacy_phases()
CONNECTIONS = legacy_connections()

SCENARIO_GROUPS: tuple[dict[str, Any], ...] = (
    {"scenario_id": "GOLDEN-TRACE", "name": "Full market-to-outcome golden trace", "phases": [phase["phase_id"] for phase in PHASES]},
    {"scenario_id": "NO-TRADE", "name": "Weak or stale opportunity remains untraded", "phases": ["1", "2.6", "3S", "3M", "3N", "3O", "3T", "3U"]},
    {"scenario_id": "RISK-BLOCK", "name": "Risk-blocked opportunity is retained and observable", "phases": ["3M", "3N", "3O", "3T", "3U"]},
    {"scenario_id": "SYNTHETIC-ISOLATION", "name": "Synthetic market cannot reach sizing, risk, or gateway", "phases": ["3R", "3M", "3N", "3T"]},
    {"scenario_id": "AUTH-INVALID-CERT", "name": "Missing or invalid Phase 3V certificate blocks new risk", "phases": ["3V"]},
    {"scenario_id": "DOMAIN-CRYPTO", "name": "Crypto domain path", "phases": ["2.7", "2.9", "3L"]},
    {"scenario_id": "DOMAIN-WEATHER", "name": "Weather domain path", "phases": ["2.8", "2.9", "3L"]},
    {"scenario_id": "DOMAIN-SPORTS", "name": "Sports domain path", "phases": ["3J", "2.9", "3L"]},
    {"scenario_id": "DOMAIN-NEWS", "name": "News and prompt-injection path", "phases": ["3I", "2.9", "3L"]},
    {"scenario_id": "DOMAIN-MICROSTRUCTURE", "name": "Microstructure stale/gap path", "phases": ["3K", "3E", "3M", "3N"]},
)


def canonical_json(value: Any) -> str:
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_text(canonical_json(value))


def stable_id(prefix: str, *parts: Any) -> str:
    text = "|".join(str(part) for part in parts)
    return f"{prefix}_{uuid.uuid5(uuid.NAMESPACE_URL, f'kalshi_predictor:phase_3w:{text}')}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_to_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value
