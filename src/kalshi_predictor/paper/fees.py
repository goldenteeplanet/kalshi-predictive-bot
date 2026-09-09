"""Reviewed fee evidence for new guarded one-contract, buy-to-settlement orders.

No production policy is certified here. Legacy configured fees are not exchange
certification. Original documents and the rate interpretation require review.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any
from urllib.parse import urlsplit

CONTRACT_KEY = "guarded_fee_contract"
CONTRACT_KIND = "guarded-single-buy-fee-v1"
PUBLIC_BASE = "https://external-api.kalshi.com/trade-api/v2"
ROUNDING_URL = "https://docs.kalshi.com/getting_started/fee_rounding"


def _bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _at(value: str | datetime) -> datetime:
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(value.replace("Z", "+00:00"))
    )
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("FEE_AWARE_CLOCK_REQUIRED")
    return result


def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("FEE_FINITE_NONNEGATIVE_VALUE_REQUIRED")
    return result


@dataclass(frozen=True)
class CertifiedFeePolicy:
    policy_id: str
    effective_from: str
    effective_to: str
    taker_rate: str
    # Ordered authoritative (URL, original SHA256) pairs include rate and rounding.
    documents: tuple[tuple[str, str], ...]
    rate_document_sha256: str
    rounding_document_sha256: str
    settlement_document_sha256: str
    settlement_fee: str
    interpretation: str = "quadratic-ceil6dp-cent-buy-zero-accumulator-v1"

    @property
    def version(self) -> str:
        return hashlib.sha256(_bytes(asdict(self))).hexdigest()


CERTIFIED_FEE_POLICIES: tuple[CertifiedFeePolicy, ...] = ()


def single_buy_fees(price: Decimal, multiplier: Decimal, rate: Decimal) -> dict[str, Decimal]:
    price, multiplier, rate = map(_decimal, (price, multiplier, rate))
    if price > 1:
        raise ValueError("FEE_PRICE_OUT_OF_RANGE")
    trade = (rate * multiplier * price * (1 - price)).quantize(
        Decimal("0.000001"), rounding=ROUND_CEILING
    )
    debit = (price + trade).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return dict(
        trade_fee=trade,
        rounding_allowance=debit - price - trade,
        estimated_fee=debit - price,
        total_debit=debit,
    )


@dataclass(frozen=True)
class FeeQuote:
    # Bytes prevent mutation between admission, risk and execution.
    payload: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    def decode(self) -> dict[str, Any]:
        return json.loads(self.payload)

    @property
    def charge(self) -> Decimal:
        return Decimal(self.decode()["simulated_charge"])


def _compute(request: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    if (
        request.get("kind") != CONTRACT_KIND
        or request.get("side") not in {"BUY_YES", "BUY_NO"}
        or type(request.get("quantity")) is not int
        or request["quantity"] != 1
    ):
        raise ValueError("ONE_CONTRACT_BUY_FEE_SCOPE_REQUIRED")
    at, current = _at(request["quoted_at"]), _at(now)
    if not 0 <= (current - at).total_seconds() <= 60:
        raise ValueError("FEE_QUOTE_STALE_OR_FUTURE")
    evidence = request["evidence"]
    matches = [p for p in CERTIFIED_FEE_POLICIES if p.version == evidence["policy_version"]]
    if len(matches) != 1:
        raise ValueError("REVIEWED_FEE_POLICY_REQUIRED")
    policy = matches[0]
    if not _at(policy.effective_from) <= at <= current < _at(policy.effective_to):
        raise ValueError("FEE_POLICY_NOT_EFFECTIVE")
    if (
        policy.interpretation != "quadratic-ceil6dp-cent-buy-zero-accumulator-v1"
        or _decimal(policy.settlement_fee) != 0
    ):
        raise ValueError("UNSUPPORTED_FEE_INTERPRETATION_OR_SETTLEMENT_CHARGE")
    docs = evidence["documents"]
    if not isinstance(docs, list) or not 1 <= len(docs) <= 6:
        raise ValueError("FEE_ORIGINAL_DOCUMENTS_REQUIRED")
    originals = []
    for document in docs:
        raw = bytes.fromhex(document["payload_hex"])
        url = document["url"]
        if (
            not 0 < len(raw) <= 3_000_000
            or _at(document["received_at"]) > at
            or urlsplit(url).scheme != "https"
            or urlsplit(url).hostname
            not in {
                "kalshi.com",
                "docs.kalshi.com",
                "assets.kalshi.com",
                "kalshi-public-docs.s3.amazonaws.com",
            }
        ):
            raise ValueError("FEE_DOCUMENT_AUTHORITY_OR_VISIBILITY_INVALID")
        originals.append((url, hashlib.sha256(raw).hexdigest()))
    if tuple(originals) != policy.documents or len(set(originals)) != len(originals):
        raise ValueError("FEE_ORIGINAL_DOCUMENT_MISMATCH")
    if (
        policy.rate_document_sha256 not in {sha for _, sha in originals}
        or policy.settlement_document_sha256 not in {sha for _, sha in originals}
        or (ROUNDING_URL, policy.rounding_document_sha256) not in originals
    ):
        raise ValueError("FEE_RATE_ROUNDING_SETTLEMENT_ORIGINAL_REQUIRED")
    captures = evidence["captures"]
    if not isinstance(captures, list) or len(captures) != 3:
        raise ValueError("EXACT_MARKET_EVENT_SERIES_FEE_ORIGINALS_REQUIRED")
    rows = {}
    for original in captures:
        raw = bytes.fromhex(original["payload_hex"])
        if not 0 < len(raw) <= 1_000_000 or hashlib.sha256(raw).hexdigest() != original["sha256"]:
            raise ValueError("FEE_CAPTURE_HASH_MISMATCH")
        row = json.loads(raw)
        if (
            row["url"] in rows
            or _at(row["received_at"]) > at
            or not 0 <= (current - _at(row["received_at"])).total_seconds() <= 60
        ):
            raise ValueError("FEE_CAPTURE_AMBIGUOUS_OR_STALE")
        rows[row["url"]] = row["body"]
    ticker = request["ticker"]
    market = rows[f"{PUBLIC_BASE}/markets/{ticker}"]["market"]
    event_id = market["event_ticker"]
    event = rows[f"{PUBLIC_BASE}/events/{event_id}"]["event"]
    series_id = event["series_ticker"]
    series = rows[f"{PUBLIC_BASE}/series/{series_id}"]["series"]
    if (
        market["ticker"] != ticker
        or event["event_ticker"] != event_id
        or series["ticker"] != series_id
        or market["status"] not in {"active", "open"}
        or not current < _at(market["close_time"])
        or series["fee_type"] != "quadratic"
        or "fee_type_override" not in event
        or "fee_multiplier_override" not in event
        or event["fee_type_override"] is not None
        or event.get("fee_multiplier_override") is not None
    ):
        raise ValueError("FEE_CATALOG_OR_OVERRIDE_INVALID")
    price, floor = _decimal(request["price"]), _decimal(request["simulator_floor"])
    fees = single_buy_fees(price, _decimal(series["fee_multiplier"]), _decimal(policy.taker_rate))
    return {
        **request,
        "event_id": event_id,
        "series": series_id,
        "fee_decomposition": {k: str(v) for k, v in fees.items()},
        "simulated_charge": str(max(fees["estimated_fee"], floor)),
        "account_precision_scope": "CONSERVATIVE_CENT_NOT_ACCOUNT_ATTESTATION",
        "initial_accumulator": "0",
        "additional_settlement_fee": "0",
    }


def build_fee_quote(
    *,
    evidence: dict[str, Any],
    ticker: str,
    side: str,
    price: Decimal,
    simulator_floor: Decimal,
    now: datetime,
) -> FeeQuote:
    request = dict(
        kind=CONTRACT_KIND,
        ticker=ticker,
        side=side,
        price=str(price),
        quantity=1,
        simulator_floor=str(simulator_floor),
        quoted_at=_at(now).isoformat(),
        evidence=evidence,
    )
    return FeeQuote(_bytes(_compute(request, now=now)))


def verify_fee_quote(
    payload: dict[str, Any],
    *,
    ticker: str,
    side: str,
    quantity: int,
    price: Decimal,
    simulator_floor: Decimal,
    now: datetime,
) -> FeeQuote:
    if not isinstance(payload, dict) or len(_bytes(payload)) > 42_000_000:
        raise ValueError("BOUNDED_FEE_CONTRACT_REQUIRED")
    keys = (
        "kind",
        "ticker",
        "side",
        "price",
        "quantity",
        "simulator_floor",
        "quoted_at",
        "evidence",
    )
    request = {key: payload[key] for key in keys}
    if (
        payload["ticker"] != ticker
        or payload["side"] != side
        or type(quantity) is not int
        or quantity != 1
        or payload["quantity"] != quantity
        or _decimal(payload["price"]) != price
        or _decimal(payload["simulator_floor"]) != simulator_floor
    ):
        raise ValueError("FEE_ORDER_OR_CONFIGURATION_MISMATCH")
    expected = _compute(request, now=now)
    if _bytes(payload) != _bytes(expected):
        raise ValueError("FEE_QUOTE_RECOMPUTATION_MISMATCH")
    return FeeQuote(_bytes(expected))


def decision_fee_quote(
    raw: dict[str, Any],
    *,
    ticker: str,
    side: str,
    quantity: int,
    price: Decimal,
    simulator_floor: Decimal,
    now: datetime,
    required: bool = False,
) -> FeeQuote | None:
    if CONTRACT_KEY not in raw:
        if required:
            raise ValueError("NEW_GUARDED_ORDER_FEE_EVIDENCE_REQUIRED")
        return None  # Legacy configured simulation, never fee certification.
    return verify_fee_quote(
        raw[CONTRACT_KEY],
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=price,
        simulator_floor=simulator_floor,
        now=now,
    )


def historical_fee_quote(
    payload: dict[str, Any], *, ticker: str, side: str, quantity: int, price: Decimal
) -> FeeQuote:
    """Reconcile pinned admission evidence, never substitute settlement-time rates."""
    return verify_fee_quote(
        payload,
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=price,
        simulator_floor=_decimal(payload["simulator_floor"]),
        now=_at(payload["quoted_at"]),
    )
