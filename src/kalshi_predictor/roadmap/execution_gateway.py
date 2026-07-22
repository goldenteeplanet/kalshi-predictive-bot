from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class LaunchEnvelope:
    environment: str
    account_id: str
    deployed_sha: str
    config_hash: str
    model_hashes: dict[str, str]
    allowed_categories: tuple[str, ...] = ("crypto", "weather")
    max_order_contracts: int = 1
    max_position_contracts_per_market: int = 1
    autopilot_enabled: bool = False
    operator_confirmation_required: bool = True
    expires_at: str | None = None
    phase_3v_approved: bool = False
    phase_3w_system_pass: bool = False
    human_approvals_complete: bool = False


@dataclass(frozen=True)
class ApprovedOrderIntent:
    intent_id: str
    ticker: str
    category: str
    side: str
    action: str
    quantity: int
    limit_price_cents: int
    phase_3n_approved: bool
    operator_confirmed: bool
    idempotency_key: str


class ExecutionGateway(Protocol):
    def account_snapshot(self) -> dict[str, Any]: ...
    def submit_order(self, intent: ApprovedOrderIntent) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def cancel_all(self) -> dict[str, Any]: ...
    def orders(self) -> list[dict[str, Any]]: ...
    def fills(self) -> list[dict[str, Any]]: ...
    def positions(self) -> list[dict[str, Any]]: ...


class DisabledExecutionGateway:
    """Default gateway. It never authenticates or performs network I/O."""

    def account_snapshot(self) -> dict[str, Any]:
        return {"status": "DISABLED", "authenticated": False}

    def submit_order(self, intent: ApprovedOrderIntent) -> dict[str, Any]:
        del intent
        raise PermissionError("Execution gateway is disabled.")

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        del order_id
        raise PermissionError("Execution gateway is disabled.")

    def cancel_all(self) -> dict[str, Any]:
        raise PermissionError("Execution gateway is disabled.")

    def orders(self) -> list[dict[str, Any]]:
        return []

    def fills(self) -> list[dict[str, Any]]:
        return []

    def positions(self) -> list[dict[str, Any]]:
        return []


class SimulatedExecutionGateway:
    """Deterministic, in-memory execution simulator with no network access."""

    mode = "SIMULATED"

    def __init__(self, *, account_id: str = "simulated-account") -> None:
        self._account_id = account_id
        self._orders: dict[str, dict[str, Any]] = {}
        self._order_ids_by_key: dict[str, str] = {}
        self._intent_fingerprints: dict[str, str] = {}
        self._fills: list[dict[str, Any]] = []
        self._positions: dict[tuple[str, str], int] = {}

    def account_snapshot(self) -> dict[str, Any]:
        return {
            "status": "READY",
            "mode": self.mode,
            "account_id": self._account_id,
            "authenticated": False,
            "network_call_performed": False,
            "order_count": len(self._orders),
            "fill_count": len(self._fills),
        }

    def submit_order(self, intent: ApprovedOrderIntent) -> dict[str, Any]:
        if intent.quantity != 1:
            raise ValueError("Simulated execution accepts exactly one contract per order.")
        if not intent.idempotency_key:
            raise ValueError("A non-empty idempotency key is required.")

        fingerprint = _intent_fingerprint(intent)
        existing_id = self._order_ids_by_key.get(intent.idempotency_key)
        if existing_id is not None:
            if self._intent_fingerprints[intent.idempotency_key] != fingerprint:
                raise ValueError("Idempotency key was already used for a different order intent.")
            return {**self._orders[existing_id], "idempotent_replay": True}

        order_id = _simulated_id("order", intent.idempotency_key)
        fill_id = _simulated_id("fill", intent.idempotency_key)
        accepted_at = _deterministic_timestamp(intent.idempotency_key)
        order = {
            "order_id": order_id,
            "client_order_id": order_id,
            "intent_id": intent.intent_id,
            "idempotency_key": intent.idempotency_key,
            "ticker": intent.ticker,
            "category": intent.category,
            "side": intent.side,
            "action": intent.action,
            "quantity": 1,
            "limit_price_cents": intent.limit_price_cents,
            "status": "FILLED",
            "mode": self.mode,
            "accepted_at": accepted_at,
            "network_call_performed": False,
            "idempotent_replay": False,
        }
        fill = {
            "fill_id": fill_id,
            "order_id": order_id,
            "ticker": intent.ticker,
            "side": intent.side,
            "action": intent.action,
            "quantity": 1,
            "price_cents": intent.limit_price_cents,
            "filled_at": accepted_at,
            "mode": self.mode,
        }
        self._orders[order_id] = order
        self._order_ids_by_key[intent.idempotency_key] = order_id
        self._intent_fingerprints[intent.idempotency_key] = fingerprint
        self._fills.append(fill)
        position_key = (intent.ticker, intent.side)
        delta = 1 if intent.action.lower() in {"buy", "purchase"} else -1
        self._positions[position_key] = self._positions.get(position_key, 0) + delta
        return dict(order)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if order is None:
            return {"order_id": order_id, "status": "NOT_FOUND", "mode": self.mode}
        if order["status"] == "FILLED":
            return {"order_id": order_id, "status": "NOT_CANCELABLE", "mode": self.mode}
        order["status"] = "CANCELED"
        return {"order_id": order_id, "status": "CANCELED", "mode": self.mode}

    def cancel_all(self) -> dict[str, Any]:
        canceled = []
        for order_id, order in self._orders.items():
            if order["status"] == "OPEN":
                order["status"] = "CANCELED"
                canceled.append(order_id)
        return {"status": "COMPLETE", "canceled_order_ids": canceled, "mode": self.mode}

    def orders(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._orders.values()]

    def fills(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._fills]

    def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "ticker": ticker,
                "side": side,
                "quantity": quantity,
                "mode": self.mode,
            }
            for (ticker, side), quantity in sorted(self._positions.items())
            if quantity != 0
        ]


def _simulated_id(kind: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return f"sim-{kind}-{digest}"


def _intent_fingerprint(intent: ApprovedOrderIntent) -> str:
    payload = json.dumps(asdict(intent), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deterministic_timestamp(idempotency_key: str) -> str:
    seconds = int(hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:8], 16)
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def authorize_intent(
    intent: ApprovedOrderIntent,
    envelope: LaunchEnvelope,
    *,
    current_environment: str,
    current_sha: str,
    unresolved_critical_incidents: int = 0,
    reconciliation_drift: bool = False,
    data_stale: bool = False,
    service_degraded: bool = False,
) -> dict[str, Any]:
    checks = {
        "phase_3n_approved": intent.phase_3n_approved,
        "operator_confirmed": intent.operator_confirmed,
        "environment_matches": envelope.environment == current_environment,
        "build_matches": envelope.deployed_sha == current_sha,
        "phase_3v_approved": envelope.phase_3v_approved,
        "phase_3w_system_pass": envelope.phase_3w_system_pass,
        "human_approvals_complete": envelope.human_approvals_complete,
        "category_allowed": intent.category in envelope.allowed_categories,
        "one_contract_order": 0 < intent.quantity <= envelope.max_order_contracts == 1,
        "autopilot_disabled": not envelope.autopilot_enabled,
        "operator_confirmation_required": envelope.operator_confirmation_required,
        "no_critical_incidents": unresolved_critical_incidents == 0,
        "no_reconciliation_drift": not reconciliation_drift,
        "data_current": not data_stale,
        "service_healthy": not service_degraded,
        "idempotency_key_present": bool(intent.idempotency_key),
    }
    return {
        "schema_version": "execution-authorization-v1",
        "authorized": all(checks.values()),
        "checks": checks,
        "blocking_reasons": [name for name, passed in checks.items() if not passed],
        "intent": asdict(intent),
        "launch_envelope": asdict(envelope),
        "network_call_performed": False,
    }
