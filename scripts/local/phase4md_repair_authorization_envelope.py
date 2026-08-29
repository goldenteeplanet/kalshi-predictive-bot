"""Issue and validate inert, single-use authorization for certified repair plans."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta

from scripts.local.phase4ma_recovery_state_machine import INVARIANTS
from scripts.local.phase4mc_repair_plan_mutation_audit import plan_errors

SCHEMA = "phase4md.repair-authorization-envelope.v1"
VALIDATION_SCHEMA = "phase4md.repair-authorization-validation.v1"
RECEIPT_SCHEMA = "phase4md.proposed-token-consumption-receipt.v1"
HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9._:-]{2,127}\Z")
MAX_LIFETIME = timedelta(minutes=15)
FIELDS = {
    "schema",
    "token_id",
    "plan_sha256",
    "incident_id_sha256",
    "recovery_epoch",
    "trusted_prefix_count",
    "trusted_checkpoint_sha256",
    "authorized_actions_sha256",
    "authorized_action_names",
    "fail_closed_invariants_sha256",
    "issuer",
    "audience",
    "issued_at",
    "expires_at",
    "nonce",
    "signature_sha256",
}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _signature(body: dict[str, object], signing_key: bytes) -> str:
    return hmac.new(signing_key, _canonical(body), hashlib.sha256).hexdigest()


def issue_token(
    plan: object,
    *,
    incident_id_sha256: str,
    recovery_epoch: int,
    issuer: str,
    audience: str,
    issued_at: str,
    expires_at: str,
    nonce: str,
    signing_key: bytes,
) -> dict[str, object]:
    errors = plan_errors(plan)
    issued = _time(issued_at)
    expires = _time(expires_at)
    if errors:
        raise ValueError("plan is not Phase 4MC-valid")
    if not isinstance(plan, dict) or plan.get("verdict") != "PASS":
        raise ValueError("only a passing repair plan can be authorized")
    if HEX64.fullmatch(incident_id_sha256) is None or type(recovery_epoch) is not int:
        raise ValueError("invalid incident identity or recovery epoch")
    if not IDENTIFIER.fullmatch(issuer) or not IDENTIFIER.fullmatch(audience):
        raise ValueError("invalid issuer or audience")
    if not IDENTIFIER.fullmatch(nonce):
        raise ValueError("invalid nonce")
    if issued is None or expires is None or expires <= issued or expires - issued > MAX_LIFETIME:
        raise ValueError("invalid token lifetime")
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        raise ValueError("signing key must contain at least 32 bytes")
    actions = plan["actions"]
    body: dict[str, object] = {
        "schema": SCHEMA,
        "token_id": _digest(
            {
                "plan_sha256": plan["plan_sha256"],
                "incident_id_sha256": incident_id_sha256,
                "recovery_epoch": recovery_epoch,
                "issuer": issuer,
                "audience": audience,
                "nonce": nonce,
            }
        ),
        "plan_sha256": plan["plan_sha256"],
        "incident_id_sha256": incident_id_sha256,
        "recovery_epoch": recovery_epoch,
        "trusted_prefix_count": plan["trusted_prefix_count"],
        "trusted_checkpoint_sha256": plan["trusted_checkpoint_sha256"],
        "authorized_actions_sha256": _digest(actions),
        "authorized_action_names": [row["action"] for row in actions],
        "fail_closed_invariants_sha256": _digest(INVARIANTS),
        "issuer": issuer,
        "audience": audience,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "nonce": nonce,
    }
    return {**body, "signature_sha256": _signature(body, signing_key)}


def validate_token(
    token: object,
    plan: object,
    *,
    incident_id_sha256: str,
    recovery_epoch: int,
    issuer: str,
    audience: str,
    evaluated_at: str,
    signing_key: bytes,
    consumed_nonces: object,
) -> dict[str, object]:
    errors: list[str] = []
    ledger_before = _digest(consumed_nonces)
    if not isinstance(token, dict) or set(token) != FIELDS:
        errors.append("TOKEN_FIELD_SET_INVALID")
        token = {}
    body = {key: value for key, value in token.items() if key != "signature_sha256"}
    if token.get("schema") != SCHEMA:
        errors.append("TOKEN_SCHEMA_INVALID")
    if not isinstance(signing_key, bytes) or len(signing_key) < 32:
        errors.append("SIGNING_KEY_INVALID")
    elif not hmac.compare_digest(str(token.get("signature_sha256")), _signature(body, signing_key)):
        errors.append("SIGNATURE_INVALID")
    if plan_errors(plan) or not isinstance(plan, dict) or plan.get("verdict") != "PASS":
        errors.append("PLAN_NOT_CERTIFIED")
        plan = {}
    expected_bindings = {
        "plan_sha256": plan.get("plan_sha256"),
        "incident_id_sha256": incident_id_sha256,
        "recovery_epoch": recovery_epoch,
        "trusted_prefix_count": plan.get("trusted_prefix_count"),
        "trusted_checkpoint_sha256": plan.get("trusted_checkpoint_sha256"),
        "authorized_actions_sha256": _digest(plan.get("actions")),
        "authorized_action_names": [
            row.get("action") for row in plan.get("actions", []) if isinstance(row, dict)
        ],
        "fail_closed_invariants_sha256": _digest(INVARIANTS),
        "issuer": issuer,
        "audience": audience,
    }
    for field, expected in expected_bindings.items():
        if token.get(field) != expected:
            errors.append(f"{field.upper()}_BINDING_MISMATCH")
    expected_token_id = _digest(
        {
            "plan_sha256": plan.get("plan_sha256"),
            "incident_id_sha256": incident_id_sha256,
            "recovery_epoch": recovery_epoch,
            "issuer": issuer,
            "audience": audience,
            "nonce": token.get("nonce"),
        }
    )
    if token.get("token_id") != expected_token_id:
        errors.append("TOKEN_ID_INVALID")
    now = _time(evaluated_at)
    issued = _time(token.get("issued_at"))
    expires = _time(token.get("expires_at"))
    if now is None or issued is None or expires is None:
        errors.append("TOKEN_TIME_INVALID")
    else:
        if expires <= issued or expires - issued > MAX_LIFETIME:
            errors.append("TOKEN_LIFETIME_INVALID")
        if now < issued:
            errors.append("TOKEN_NOT_YET_VALID")
        if now >= expires:
            errors.append("TOKEN_EXPIRED")
    nonce = token.get("nonce")
    if not isinstance(consumed_nonces, (set, frozenset, list, tuple)) or any(
        not isinstance(value, str) for value in consumed_nonces
    ):
        errors.append("CONSUMED_NONCE_LEDGER_INVALID")
        consumed_nonces = []
    if not isinstance(nonce, str) or IDENTIFIER.fullmatch(nonce) is None:
        errors.append("NONCE_INVALID")
    elif nonce in consumed_nonces:
        errors.append("TOKEN_REPLAYED")
    errors = sorted(set(errors))
    receipt = None
    if not errors:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "token_id": token["token_id"],
            "nonce_sha256": hashlib.sha256(str(nonce).encode()).hexdigest(),
            "plan_sha256": token["plan_sha256"],
            "validated_at": evaluated_at,
            "persistence_required_before_execution": True,
            "execution_authorized_by_validator": False,
        }
        receipt["receipt_sha256"] = _digest(receipt)
    result: dict[str, object] = {
        "schema": VALIDATION_SCHEMA,
        "verdict": "PASS" if not errors else "REFUSE",
        "errors": errors,
        "token_id": token.get("token_id"),
        "proposed_consumption_receipt": receipt,
        "consumed_nonce_ledger_unchanged": _digest(consumed_nonces) == ledger_before,
        "safety": {
            "validation_only": True,
            "token_persistence": False,
            "repair_execution": False,
            "runtime_write": False,
            "wsl_control": False,
            "service_control": False,
            "network_access": False,
            "order_capability": False,
        },
    }
    result["validation_sha256"] = _digest(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("token")
    parser.add_argument("plan")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--recovery-epoch", required=True, type=int)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--evaluated-at", required=True)
    parser.add_argument("--signing-key-hex", required=True)
    parser.add_argument("--consumed-nonce", action="append", default=[])
    args = parser.parse_args()
    with open(args.token, encoding="utf-8") as stream:
        token = json.load(stream)
    with open(args.plan, encoding="utf-8") as stream:
        plan = json.load(stream)
    try:
        key = bytes.fromhex(args.signing_key_hex)
    except ValueError:
        key = b""
    result = validate_token(
        token,
        plan,
        incident_id_sha256=args.incident_id,
        recovery_epoch=args.recovery_epoch,
        issuer=args.issuer,
        audience=args.audience,
        evaluated_at=args.evaluated_at,
        signing_key=key,
        consumed_nonces=args.consumed_nonce,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
