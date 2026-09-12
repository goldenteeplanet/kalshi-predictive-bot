"""Original-bound SOL CF analytical bridge; no forecast or rule certification.

Target assumptions remain explicitly uncertified. Revalidation reconstructs
process inputs from original bytes at both clocks; it never predicts an outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_predictor.crypto.cf_process_inputs import MAX_BYTES, digest, estimate_cf_process
from kalshi_predictor.crypto.settlement_target import SettlementBenchmarkTarget, aware
from kalshi_predictor.overnight_paper.provenance import canonical_hash

CLOCK_BASIS = "cf-sol-original-observation-receipt-v1"
BUNDLE_URL = "urn:kalshi-paper:cf-sol-analytical-v1"
VERIFIER = "cf-sol-original-analytical-v1"


@dataclass(frozen=True)
class CFSourceContext:
    target: SettlementBenchmarkTarget

    def to_record(self, *, decision_at: datetime) -> dict[str, Any]:
        """Preserve declared target originals for durable replay, not authority."""
        return dict(
            schema="cf-sol-target-context-v1",
            target=self.target.validate(as_of=decision_at, include_sol_rule_binding=True),
            rule_original={
                "sha256": digest(self.target.rule_original),
                "payload_hex": self.target.rule_original.hex(),
            },
            market_original={
                "sha256": digest(self.target.market_original),
                "payload_hex": self.target.market_original.hex(),
            },
            rule_received_at=self.target.rule_received_at.isoformat(),
            market_received_at=self.target.market_received_at.isoformat(),
        )

    @classmethod
    def from_record(cls, record: dict[str, Any], *, decision_at: datetime) -> CFSourceContext:
        from kalshi_predictor.crypto.cf_settlement_windows import CFWindow, CFWindowRules
        from kalshi_predictor.overnight_paper.source_health import aware as parse_time

        if type(record) is not dict or record.get("schema") != "cf-sol-target-context-v1":
            raise ValueError("CF_TARGET_CONTEXT_RECORD_REQUIRED")
        declared = record["target"]
        rules = dict(declared["rules"])
        rules["closing"] = CFWindow(**rules["closing"])
        if rules["opening"] is not None:
            rules["opening"] = CFWindow(**rules["opening"])
        target = SettlementBenchmarkTarget(
            symbol=declared["symbol"], event_ticker=declared["event_ticker"],
            rules=CFWindowRules(**rules), comparator=declared["comparator"],
            threshold=None if declared["threshold"] is None else Decimal(declared["threshold"]),
            lower=None if declared["lower"] is None else Decimal(declared["lower"]),
            upper=None if declared["upper"] is None else Decimal(declared["upper"]),
            rule_original=_original(record["rule_original"]),
            market_original=_original(record["market_original"]),
            rule_received_at=parse_time(record["rule_received_at"]),
            market_received_at=parse_time(record["market_received_at"]),
            finality_deadline=(
                None if declared["finality_deadline"] is None
                else parse_time(declared["finality_deadline"])
            ),
            finality_basis=declared["finality_basis"],
        )
        if target.symbol != "SOL":
            raise ValueError("CF_BRIDGE_SOL_TARGET_REQUIRED")
        restored = cls(target)
        if canonical_hash(restored.to_record(decision_at=decision_at)) != canonical_hash(record):
            raise ValueError("CF_TARGET_CONTEXT_RECONSTRUCTION_MISMATCH")
        return restored


def _original(item: dict[str, Any]) -> bytes:
    if type(item) is not dict or set(item) != {"sha256", "payload_hex"}:
        raise ValueError("CF_BRIDGE_ORIGINAL_REQUIRED")
    encoded = item["payload_hex"]
    if type(encoded) is not str or not 0 < len(encoded) <= 2 * MAX_BYTES:
        raise ValueError("CF_BRIDGE_ORIGINAL_SIZE")
    raw = bytes.fromhex(encoded)
    if digest(raw) != item["sha256"]:
        raise ValueError("CF_BRIDGE_ORIGINAL_HASH")
    return raw


def _view(
    raw: bytes, receipt: bytes, target: SettlementBenchmarkTarget,
    decision_at: datetime, now: datetime,
) -> dict[str, Any]:
    aware(decision_at)
    aware(now)
    if now < decision_at:
        raise ValueError("CF_BRIDGE_REVALIDATION_BEFORE_DECISION")
    if type(target) is not SettlementBenchmarkTarget or target.symbol != "SOL":
        raise ValueError("CF_BRIDGE_SOL_TARGET_REQUIRED")
    declared = target.validate(as_of=decision_at, include_sol_rule_binding=True)
    frozen = estimate_cf_process(
        raw, receipt, as_of=decision_at,
        source_sha256=digest(raw), receipt_sha256=digest(receipt), target=target,
    )
    # This rechecks observation freshness and the prewindow requirement at use.
    # Do not replace the original decision-time estimate with a later estimate.
    estimate_cf_process(
        raw, receipt, as_of=now,
        source_sha256=digest(raw), receipt_sha256=digest(receipt), target=target,
    )
    process = frozen.process
    return dict(
        target=declared,
        inputs=dict(
            symbol=process.symbol, index_id=process.index_id,
            level=str(process.level),
            observed_at=process.level_observed_at.isoformat(),
            volatility_per_sqrt_minute=process.volatility_per_sqrt_minute,
            evidence=frozen.evidence,
        ),
    )


def build_cf_source(
    *, raw: bytes, receipt: bytes, target: SettlementBenchmarkTarget,
    decision_at: datetime,
) -> dict[str, Any]:
    """Build an analytical bundle from saved originals, without I/O or forecasting."""
    view = _view(raw, receipt, target, decision_at, decision_at)
    return dict(
        url=BUNDLE_URL, clock_basis=CLOCK_BASIS, role="ANALYTICAL_SOURCE",
        settlement_truth=False, provider_updated_at=None, provider_generated_at=None,
        available_at=view["inputs"]["evidence"]["received_at"],
        received_at=view["inputs"]["evidence"]["received_at"],
        decision_at=decision_at.isoformat(),
        body={
            "cf": {"sha256": digest(raw), "payload_hex": raw.hex()},
            "receipt": {"sha256": digest(receipt), "payload_hex": receipt.hex()},
        },
        **view,
    )


def verify_cf_source(
    source: dict[str, Any], *, target: SettlementBenchmarkTarget,
    decision_at: datetime, now: datetime,
) -> dict[str, Any]:
    """Compare the complete bundle to reconstruction, including null provider clocks."""
    if type(source) is not dict or type(source.get("body")) is not dict:
        raise ValueError("CF_BRIDGE_BUNDLE_REQUIRED")
    if set(source["body"]) != {"cf", "receipt"}:
        raise ValueError("CF_BRIDGE_ORIGINAL_PAIR_REQUIRED")
    raw, receipt = (_original(source["body"][key]) for key in ("cf", "receipt"))
    view = _view(raw, receipt, target, decision_at, now)
    expected = build_cf_source(raw=raw, receipt=receipt, target=target, decision_at=decision_at)
    if canonical_hash(source) != canonical_hash(expected):
        raise ValueError("CF_BRIDGE_RECONSTRUCTION_MISMATCH")
    return dict(
        **view, input_sha256=canonical_hash(view),
        available_at=expected["available_at"],
        source_sha256=canonical_hash(source),
        execution_authority=False,
    )


def verify_cf_binding(
    source: dict[str, Any], *, context: CFSourceContext,
    decision: dict[str, Any], now: datetime, forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the analytical check to an exact SOL decision, not rule authority."""
    if type(context) is not CFSourceContext:
        raise ValueError("CF_BRIDGE_CONTEXT_REQUIRED")
    from kalshi_predictor.overnight_paper.source_health import aware as parse_time

    checked = verify_cf_source(
        source, target=context.target,
        decision_at=parse_time(decision["decision_at"]), now=now,
    )
    expected = {
        "category": "Crypto", "series": "KXSOLE", "source_kind": CLOCK_BASIS,
        "ticker": context.target.rules.market_ticker,
        "event_id": context.target.event_ticker,
        "cf_input_sha256": checked["input_sha256"],
        "cf_target_sha256": canonical_hash(checked["target"]),
    }
    if any(decision.get(key) != value for key, value in expected.items()):
        raise ValueError("CF_BRIDGE_DECISION_BINDING")
    hashes = decision.get("source_hashes")
    if type(hashes) is not list or hashes.count(checked["source_sha256"]) != 1:
        raise ValueError("CF_BRIDGE_DECISION_SOURCE_REQUIRED")
    if forecast is not None and (
        any(forecast.get(key) != value for key, value in expected.items())
        or not parse_time(checked["available_at"])
        <= parse_time(forecast["generated_at"])
        <= parse_time(decision["decision_at"])
    ):
        raise ValueError("CF_BRIDGE_FORECAST_INPUT_BINDING")
    return checked


def cf_feature_record(
    source: dict[str, Any], *, context: CFSourceContext,
    decision_at: datetime, now: datetime,
) -> dict[str, Any]:
    checked = verify_cf_source(source, target=context.target, decision_at=decision_at, now=now)
    return dict(
        name="cf_sol_process_inputs", value=checked["inputs"],
        source_sha256=checked["source_sha256"],
        observed_at=checked["inputs"]["observed_at"], available_at=checked["available_at"],
    )
