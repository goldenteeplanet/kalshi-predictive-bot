# ruff: noqa: E501

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from kalshi_predictor.system_certification.phase_registry import MANDATORY_PHASE_IDS

CONNECTION_REGISTRY_VERSION = "phase_3w_r_typed_connection_registry_v1"

ENDPOINT_PHASE = "phase"
ENDPOINT_PHASE_GROUP = "phase_group"
ENDPOINT_PLATFORM_SERVICE = "platform_service"

PLATFORM_GATEWAY = "ORDER_GATEWAY"
PLATFORM_OBSERVABILITY = "OBSERVABILITY"
PLATFORM_BACKEND_AUTHORITIES = "BACKEND_AUTHORITIES"
PLATFORM_PHASE_3W_CERTIFIER = "3W"

ALLOWED_ENDPOINT_KINDS = {
    ENDPOINT_PHASE,
    ENDPOINT_PHASE_GROUP,
    ENDPOINT_PLATFORM_SERVICE,
}

ALLOWED_PLATFORM_SERVICES = {
    PLATFORM_GATEWAY,
    PLATFORM_OBSERVABILITY,
    PLATFORM_BACKEND_AUTHORITIES,
    PLATFORM_PHASE_3W_CERTIFIER,
}

DURABLE_PHASE_IDS: tuple[str, ...] = MANDATORY_PHASE_IDS


@dataclass(frozen=True)
class Endpoint:
    kind: str
    refs: tuple[str, ...]
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConnectionRegistryEntry:
    connection_id: str
    producer: Endpoint
    consumer: Endpoint
    transport: str
    contract: str
    required: bool = True
    negative_assertion: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "producer": self.producer.to_dict(),
            "consumer": self.consumer.to_dict(),
            "producer_display": render_endpoint(self.producer),
            "consumer_display": render_endpoint(self.consumer),
            "transport": self.transport,
            "contract": self.contract,
            "required": self.required,
            "negative_assertion": self.negative_assertion,
            "notes": self.notes,
            "expanded_instances": expand_connection_instances(self),
        }


def phase(phase_id: str) -> Endpoint:
    return Endpoint(kind=ENDPOINT_PHASE, refs=(phase_id,))


def phase_group(refs: tuple[str, ...], *, label: str = "") -> Endpoint:
    return Endpoint(kind=ENDPOINT_PHASE_GROUP, refs=refs, label=label)


def platform(ref: str, *, label: str = "") -> Endpoint:
    return Endpoint(kind=ENDPOINT_PLATFORM_SERVICE, refs=(ref,), label=label)


def edge(
    connection_id: str,
    producer: Endpoint,
    consumer: Endpoint,
    transport: str,
    contract: str,
    *,
    negative_assertion: bool = False,
    notes: str = "",
) -> ConnectionRegistryEntry:
    return ConnectionRegistryEntry(
        connection_id=connection_id,
        producer=producer,
        consumer=consumer,
        transport=transport,
        contract=contract,
        negative_assertion=negative_assertion,
        notes=notes,
    )


CONNECTION_REGISTRY: tuple[ConnectionRegistryEntry, ...] = (
    edge("E001", phase("1"), phase("2"), "table", "Canonical quote/market/settlement semantics"),
    edge("E002", phase("1"), phase("2.5"), "table", "Immutable snapshots and availability times"),
    edge("E003", phase("1"), phase("2.6"), "table", "Eligibility, lifecycle, executable quote, freshness"),
    edge("E004", phase("1"), phase("2.7"), "table", "Crypto market mapping and cutoff"),
    edge("E005", phase("1"), phase("2.8"), "table", "Weather location/rule mapping"),
    edge("E006", phase("1"), phase("3H"), "table", "Close and settlement timing"),
    edge("E007", phase("1"), phase("3I"), "table", "Market/entity mapping"),
    edge("E008", phase("1"), phase("3J"), "table", "Sports event/market mapping"),
    edge("E009", phase("1"), phase("3K"), "table", "Synchronized book and trade feed"),
    edge("E010", phase("1"), phase("3O"), "table", "Market/snapshot/settlement lineage"),
    edge("E011", phase("2"), phase("3O"), "table", "Paper intent/order/fill/outcome lineage"),
    edge("E012", phase("2"), phase("3P"), "table", "Settled realized results"),
    edge("E013", phase("2"), phase("3Q"), "table", "Full decision universe and net outcomes"),
    edge("E014", phase("2"), phase("3S"), "table", "Behavior decisions, propensities/support, rewards"),
    edge("E015", phase("2.5"), phase("2.7"), "function", "Point-in-time crypto features"),
    edge("E016", phase("2.5"), phase("2.8"), "function", "Point-in-time weather features"),
    edge("E017", phase("2.5"), phase("2.9"), "function", "Frozen evaluation dataset"),
    edge("E018", phase("2.5"), phase("3F"), "table", "Closed-outcome calibration data"),
    edge("E019", phase("2.5"), phase("3L"), "function", "Online/offline-compatible features"),
    edge("E020", phase("2.5"), phase("3Q"), "table", "Eligible feature universe"),
    edge("E021", phase("2.5"), phase("3S"), "table", "Context/reward evaluation data"),
    edge("E022", phase("2.7"), phase("3L"), "function", "Crypto prediction contract"),
    edge("E023", phase("2.8"), phase("3L"), "function", "Weather prediction contract"),
    edge("E024", phase("3I"), phase("3L"), "function", "News feature contract and trust boundary"),
    edge("E025", phase("3J"), phase("3L"), "function", "Sports feature/prediction contract"),
    edge("E026", phase("3K"), phase("3M"), "function", "Liquidity, spread, slippage, quality"),
    edge("E027", phase("2.9"), phase("3L"), "function", "Ensemble version/member contribution"),
    edge("E028", phase("3F"), phase("3M"), "table", "Calibrated confidence contract"),
    edge("E029", phase("3L"), phase("2.6"), "table", "Final probability and uncertainty"),
    edge("E030", phase("2.6"), phase("3E"), "function", "Candidate universe and executable economics"),
    edge("E031", phase("3H"), phase("3S"), "function", "Specialized candidate conforms to normal contract"),
    edge("E032", phase("3E"), phase("3S"), "function", "Net opportunity state"),
    edge("E033", phase("3S"), phase("3M"), "function", "Only PROCEED reaches sizing"),
    edge("E034", phase("3M"), phase("3N"), "function", "Immutable size proposal and reasons"),
    edge("E035", phase("3N"), phase("3A"), "function", "Approved demo intent only"),
    edge("E036", phase("3N"), phase("3B"), "function", "Approved guarded intent and reservation"),
    edge("E037", phase("3N"), phase("3U"), "function", "Accurate risk result and approved size"),
    edge("E038", phase("3B"), phase("3V"), "function", "Live intent scope and build fingerprint"),
    edge("E039", phase("3V"), platform(PLATFORM_GATEWAY, label="Gateway"), "function", "Valid current certificate and launch envelope"),
    edge("E040", phase("2"), phase("3O"), "table", "Intent/order/fill/position lineage"),
    edge("E041", phase("1"), phase("2"), "table", "Idempotent settlement and payout"),
    edge("E042", phase("2"), phase("3O"), "table", "Final outcome and P&L"),
    edge("E043", phase("3O"), phase("3P"), "table", "Complete nightly evaluation dataset"),
    edge("E044", phase("3O"), phase("3Q"), "table", "Point-in-time feature/outcome dataset"),
    edge("E045", phase("3O"), phase("3S"), "table", "Historical contexts/actions/rewards"),
    edge("E046", phase("3O"), phase("3T"), "table", "Typed read models, freshness, trace IDs"),
    edge("E047", phase("3O"), phase("3U"), "table", "Current scope and historical context"),
    edge("E048", phase("3R"), phase("3O"), "table", "Synthetic memory with non-tradable flag"),
    edge("E049", phase("3R"), phase("3T"), "table", "Clearly synthetic read contract"),
    edge("E050", phase("3R"), phase("3M"), "negative_assertion", "Synthetic cannot reach sizing/risk/gateway", negative_assertion=True),
    edge("E051", phase("3P"), phase("3T"), "table", "Journal read model only"),
    edge("E052", phase("3Q"), phase("3T"), "table", "Candidate only, no auto promotion"),
    edge("E053", phase("3S"), phase("3T"), "table", "Policy artifact only, shadow first"),
    edge(
        "E054",
        platform(PLATFORM_PHASE_3W_CERTIFIER, label="3W"),
        phase("3V"),
        "manifest",
        "Phase 3W evidence manifest and explicit limitations for Phase 3V",
        notes="Certification evidence handoff; this does not authorize live trading.",
    ),
    edge(
        "E055",
        phase_group(("3C", "3D", "3T", "3U"), label="3C/3D/3T/3U"),
        platform(PLATFORM_BACKEND_AUTHORITIES, label="backend authorities"),
        "negative_assertion",
        "Read-only and authorized request contracts only",
        negative_assertion=True,
        notes="Dashboard/workstation/advisory surfaces may read or request, but may not bypass 3M/3N/3V authorities.",
    ),
    edge(
        "E056",
        phase("3G"),
        phase_group(DURABLE_PHASE_IDS, label="all durable phases"),
        "multiple",
        "Transactions, constraints, idempotency, backup, restore, and migration ancestry",
        notes="Database hardening supports every durable phase rather than one observability target.",
    ),
    edge(
        "E057",
        phase_group(MANDATORY_PHASE_IDS, label="all phases"),
        platform(PLATFORM_OBSERVABILITY, label="observability"),
        "multiple",
        "Correlation, causation, freshness, and health signals",
        notes="Observability is a platform service consumed by every phase.",
    ),
)


def connection_registry_entries() -> tuple[ConnectionRegistryEntry, ...]:
    return CONNECTION_REGISTRY


def validate_connection_registry(
    entries: tuple[ConnectionRegistryEntry, ...] = CONNECTION_REGISTRY,
) -> list[str]:
    errors: list[str] = []
    ids = [entry.connection_id for entry in entries]
    duplicates = sorted({connection_id for connection_id in ids if ids.count(connection_id) > 1})
    if duplicates:
        errors.append(f"duplicate connection ids: {', '.join(duplicates)}")
    if len(entries) != 57:
        errors.append(f"expected 57 connections, found {len(entries)}")
    for row in entries:
        errors.extend(_validate_endpoint(row.connection_id, "producer", row.producer))
        errors.extend(_validate_endpoint(row.connection_id, "consumer", row.consumer))
    return errors


def connection_registry_payload() -> dict[str, Any]:
    return {
        "registry_version": CONNECTION_REGISTRY_VERSION,
        "connection_count": len(CONNECTION_REGISTRY),
        "validation_errors": validate_connection_registry(),
        "connections": [entry.to_dict() for entry in CONNECTION_REGISTRY],
    }


def legacy_connections() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "connection_id": row.connection_id,
            "source_phase": render_endpoint(row.producer),
            "destination_phase": render_endpoint(row.consumer),
            "transport": row.transport,
            "contract": row.contract,
        }
        for row in CONNECTION_REGISTRY
    )


def render_endpoint(endpoint: Endpoint) -> str:
    if endpoint.label:
        return endpoint.label
    if endpoint.kind == ENDPOINT_PHASE:
        return endpoint.refs[0]
    return "/".join(endpoint.refs)


def expand_connection_instances(entry: ConnectionRegistryEntry) -> list[dict[str, str]]:
    producers = _expand_endpoint(entry.producer)
    consumers = _expand_endpoint(entry.consumer)
    instances: list[dict[str, str]] = []
    for producer_ref in producers:
        for consumer_ref in consumers:
            instances.append(
                {
                    "producer": producer_ref,
                    "consumer": consumer_ref,
                    "display": f"{producer_ref} -> {consumer_ref}",
                }
            )
    return instances


def _expand_endpoint(endpoint: Endpoint) -> tuple[str, ...]:
    return endpoint.refs


def _validate_endpoint(connection_id: str, role: str, endpoint: Endpoint) -> list[str]:
    errors: list[str] = []
    if endpoint.kind not in ALLOWED_ENDPOINT_KINDS:
        errors.append(f"{connection_id}: {role} has invalid kind {endpoint.kind}")
        return errors
    if not endpoint.refs:
        errors.append(f"{connection_id}: {role} has no refs")
    if endpoint.kind == ENDPOINT_PLATFORM_SERVICE:
        unknown_platforms = sorted(set(endpoint.refs) - ALLOWED_PLATFORM_SERVICES)
        if unknown_platforms:
            errors.append(
                f"{connection_id}: {role} has unknown platform service(s) "
                f"{', '.join(unknown_platforms)}"
            )
        return errors
    unknown_phases = sorted(set(endpoint.refs) - set(MANDATORY_PHASE_IDS))
    if unknown_phases:
        errors.append(
            f"{connection_id}: {role} has unknown phase ref(s) {', '.join(unknown_phases)}"
        )
    return errors
