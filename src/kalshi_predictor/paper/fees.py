"""Reviewed fee evidence for new guarded one-contract, buy-to-settlement orders.

Local policy review supplies exchange-fee evidence only, never trading authority.
Legacy configured fees are not exchange certification.
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
EVENT_SCHEMA_URL = "https://docs.kalshi.com/openapi.yaml"
EVENT_DATA_SCHEMA_SHA256 = "11aa0ec82b47bc186a52fb8d8bd1a16cd876edad42dd9749625f2a1b78a92c38"
LEGACY_EVENT_OVERRIDE_PROFILE = "explicit-null-pair-v1"
OPTIONAL_EVENT_OVERRIDE_PROFILE = "event-data-optional-pair-v1"


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
    event_override_interpretation: str = LEGACY_EVENT_OVERRIDE_PROFILE
    event_schema_document_sha256: str | None = None
    permitted_series: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        scope = self.permitted_series
        if scope is not None and (
            type(scope) is not tuple
            or not scope
            or any(
                not isinstance(item, str)
                or not item
                or not item.isascii()
                or not item.isalnum()
                or item != item.upper()
                for item in scope
            )
            or tuple(sorted(set(scope))) != scope
        ):
            raise ValueError("FEE_EXACT_SERIES_SCOPE_REQUIRED")

    @property
    def version(self) -> str:
        fields = asdict(self)
        if self.permitted_series is None:
            # An absent restriction preserves previously reviewed policy identities.
            fields.pop("permitted_series")
        if (
            self.event_override_interpretation == LEGACY_EVENT_OVERRIDE_PROFILE
            and self.event_schema_document_sha256 is None
        ):
            # Preserve every existing policy hash and historical quote byte contract.
            fields.pop("event_override_interpretation")
            fields.pop("event_schema_document_sha256")
        return hashlib.sha256(_bytes(fields)).hexdigest()


# Reviewed original July 7 schedule, fee-rounding documentation and event schema.
# One-hour operational review window; not a promise of unchanged future fees.
# Exchange-only immediate single buy held to settlement; excludes FCM add-ons.
CERTIFIED_FEE_POLICIES: tuple[CertifiedFeePolicy, ...] = (
    CertifiedFeePolicy(
        policy_id="kxbtc-kxtempmiah-exchange-only-single-buy-20260911",
        effective_from="2026-09-11T03:35:00+00:00",
        effective_to="2026-09-11T04:23:28.844889+00:00",
        taker_rate="0.07",
        documents=(
            ("https://kalshi.com/docs/kalshi-fee-schedule.pdf",
             "c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601"),
            (ROUNDING_URL,
             "6b509a24b136624756bd16d74586f04c3e603ca26544ea28ceb9533afda55608"),
            (EVENT_SCHEMA_URL, EVENT_DATA_SCHEMA_SHA256),
        ),
        rate_document_sha256="c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601",
        rounding_document_sha256="6b509a24b136624756bd16d74586f04c3e603ca26544ea28ceb9533afda55608",
        settlement_document_sha256="c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601",
        settlement_fee="0",
        event_override_interpretation=OPTIONAL_EVENT_OVERRIDE_PROFILE,
        event_schema_document_sha256=EVENT_DATA_SCHEMA_SHA256,
        permitted_series=("KXBTC", "KXTEMPMIAH"),
    ),
    # Separately reviewed renewal; historical policy and identity remain unchanged.
    CertifiedFeePolicy(
        policy_id="kxbtc-kxtempmiah-exchange-only-single-buy-20260911-0450",
        effective_from="2026-09-11T04:50:00+00:00",
        effective_to="2026-09-11T05:50:00+00:00",
        taker_rate="0.07",
        documents=(
            ("https://kalshi.com/docs/kalshi-fee-schedule.pdf",
             "c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601"),
            (ROUNDING_URL,
             "6b509a24b136624756bd16d74586f04c3e603ca26544ea28ceb9533afda55608"),
            (EVENT_SCHEMA_URL, EVENT_DATA_SCHEMA_SHA256),
        ),
        rate_document_sha256="c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601",
        rounding_document_sha256="6b509a24b136624756bd16d74586f04c3e603ca26544ea28ceb9533afda55608",
        settlement_document_sha256="c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601",
        settlement_fee="0",
        event_override_interpretation=OPTIONAL_EVENT_OVERRIDE_PROFILE,
        event_schema_document_sha256=EVENT_DATA_SCHEMA_SHA256,
        permitted_series=("KXBTC", "KXTEMPMIAH"),
    ),
)


def _strict_event_json(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("FEE_EVENT_DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def nonfinite(value: str) -> None:
        raise ValueError("FEE_EVENT_NONFINITE_JSON")

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=nonfinite)
    if not isinstance(value, dict):
        raise ValueError("FEE_FULL_EVENT_RESPONSE_REQUIRED")
    return value


def _optional_event_override_state(
    event: dict[str, Any],
    *,
    evidence: dict[str, Any],
    event_url: str,
    captured_body: dict[str, Any],
    market: dict[str, Any],
    received_at: datetime,
    quoted_at: datetime,
    current: datetime,
    policy: CertifiedFeePolicy,
    documents: list[tuple[str, str]],
) -> str:
    if (
        policy.event_override_interpretation != OPTIONAL_EVENT_OVERRIDE_PROFILE
        or policy.event_schema_document_sha256 != EVENT_DATA_SCHEMA_SHA256
        or (EVENT_SCHEMA_URL, EVENT_DATA_SCHEMA_SHA256) not in documents
    ):
        raise ValueError("FEE_REVIEWED_EVENT_SCHEMA_REQUIRED")
    original = evidence.get("event_original")
    if not isinstance(original, dict):
        raise ValueError("FEE_RAW_EVENT_ORIGINAL_REQUIRED")
    raw = bytes.fromhex(original["payload_hex"])
    if (
        not 0 < len(raw) <= 1_000_000
        or hashlib.sha256(raw).hexdigest() != original.get("sha256")
        or original.get("url") != event_url
        or type(original.get("status")) is not int
        or original["status"] != 200
        or _at(original["received_at"]) != received_at
        or not received_at <= quoted_at <= current
        or not 0 <= (current - received_at).total_seconds() <= 60
    ):
        raise ValueError("FEE_RAW_EVENT_ORIGINAL_MISMATCH")
    body = _strict_event_json(raw)
    if _bytes(body) != _bytes(captured_body):
        raise ValueError("FEE_RAW_EVENT_BODY_MAPPING_MISMATCH")
    if (
        not isinstance(body.get("event"), dict)
        or not isinstance(body.get("markets"), list)
        or any(not isinstance(market, dict) for market in body["markets"])
    ):
        raise ValueError("FEE_FULL_EVENT_RESPONSE_REQUIRED")
    required_strings = (
        "event_ticker",
        "series_ticker",
        "sub_title",
        "title",
        "collateral_return_type",
    )
    sources = event.get("settlement_sources")
    if (
        any(not isinstance(event.get(key), str) for key in required_strings)
        or not event["event_ticker"]
        or not event["series_ticker"]
        or type(event.get("mutually_exclusive")) is not bool
        or "settlement_sources" not in event
        or (sources is not None and not isinstance(sources, list))
    ):
        raise ValueError("FEE_EVENT_DATA_SCHEMA_REQUIRED_FIELDS")
    for source in sources or []:
        if not isinstance(source, dict) or any(
            key in source and not isinstance(source[key], str) for key in ("name", "url")
        ):
            raise ValueError("FEE_EVENT_SETTLEMENT_SOURCE_SCHEMA")
    keys = {"fee_type_override", "fee_multiplier_override"}

    def reject_unknown_fee_fields(
        node: Any,
        *,
        event_root: bool = False,
        market_root: bool = False,
    ) -> None:
        if isinstance(node, dict):
            if market_root:
                market_pair = keys.intersection(node)
                if market_pair and (
                    market_pair != keys or any(node[key] is not None for key in keys)
                ):
                    raise ValueError("FEE_MARKET_OVERRIDE_UNSUPPORTED")
                if node.get("fee_waiver_expiration_time") is not None:
                    raise ValueError("FEE_MARKET_WAIVER_UNSUPPORTED")
            for key, value in node.items():
                known_empty_market_field = market_root and key in keys | {
                    "fee_waiver_expiration_time"
                }
                if "fee" in key.lower() and not (
                    (event_root and key in keys) or known_empty_market_field
                ):
                    raise ValueError("FEE_UNKNOWN_EVENT_FEE_FIELD")
                if event_root and key == "markets":
                    if not isinstance(value, list) or any(
                        not isinstance(item, dict) for item in value
                    ):
                        raise ValueError("FEE_EVENT_NESTED_MARKETS_SCHEMA")
                    for item in value:
                        reject_unknown_fee_fields(item, market_root=True)
                else:
                    reject_unknown_fee_fields(value)
        elif isinstance(node, list):
            for value in node:
                reject_unknown_fee_fields(value)

    reject_unknown_fee_fields(market, market_root=True)
    for key, value in body.items():
        if "fee" in key.lower():
            raise ValueError("FEE_UNKNOWN_EVENT_FEE_FIELD")
        if key == "markets":
            for item in value:
                reject_unknown_fee_fields(item, market_root=True)
        else:
            reject_unknown_fee_fields(value, event_root=key == "event")
    present = keys.intersection(event)
    if not present:
        return "INHERIT_SERIES_OMITTED_PAIR"
    if present != keys:
        raise ValueError("FEE_PARTIAL_EVENT_OVERRIDE_PAIR")
    if all(event[key] is None for key in keys):
        return "INHERIT_SERIES_EXPLICIT_NULL_PAIR"
    raise ValueError("FEE_EVENT_OVERRIDE_UNSUPPORTED")


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
    optional_event_profile = policy.event_override_interpretation == OPTIONAL_EVENT_OVERRIDE_PROFILE
    if not optional_event_profile and (
        policy.event_override_interpretation != LEGACY_EVENT_OVERRIDE_PROFILE
        or policy.event_schema_document_sha256 is not None
    ):
        raise ValueError("FEE_EVENT_OVERRIDE_PROFILE_UNSUPPORTED")
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
    received_times = {}
    for original in captures:
        raw = bytes.fromhex(original["payload_hex"])
        if not 0 < len(raw) <= 1_000_000 or hashlib.sha256(raw).hexdigest() != original["sha256"]:
            raise ValueError("FEE_CAPTURE_HASH_MISMATCH")
        row = _strict_event_json(raw) if optional_event_profile else json.loads(raw)
        if (
            row["url"] in rows
            or _at(row["received_at"]) > at
            or not 0 <= (current - _at(row["received_at"])).total_seconds() <= 60
        ):
            raise ValueError("FEE_CAPTURE_AMBIGUOUS_OR_STALE")
        rows[row["url"]] = row["body"]
        received_times[row["url"]] = _at(row["received_at"])
    ticker = request["ticker"]
    market = rows[f"{PUBLIC_BASE}/markets/{ticker}"]["market"]
    event_id = market["event_ticker"]
    event_url = f"{PUBLIC_BASE}/events/{event_id}"
    event_body = rows[event_url]
    if optional_event_profile and (
        not isinstance(event_body, dict) or not isinstance(event_body.get("event"), dict)
    ):
        raise ValueError("FEE_FULL_EVENT_RESPONSE_REQUIRED")
    event = event_body["event"]
    optional_fields = {}
    if optional_event_profile:
        override_state = _optional_event_override_state(
            event,
            evidence=evidence,
            event_url=event_url,
            captured_body=event_body,
            market=market,
            received_at=received_times[event_url],
            quoted_at=at,
            current=current,
            policy=policy,
            documents=originals,
        )
        optional_fields = {
            "event_override_state": override_state,
            "event_override_interpretation": policy.event_override_interpretation,
            "event_schema_document_sha256": policy.event_schema_document_sha256,
        }
    series_id = event["series_ticker"]
    series = rows[f"{PUBLIC_BASE}/series/{series_id}"]["series"]
    if policy.permitted_series is not None and series_id not in policy.permitted_series:
        raise ValueError("FEE_SERIES_OUTSIDE_REVIEWED_SCOPE")
    if (
        market["ticker"] != ticker
        or ("series_ticker" in market and market["series_ticker"] != series_id)
        or event["event_ticker"] != event_id
        or series["ticker"] != series_id
        or market["status"] not in {"active", "open"}
        or not current < _at(market["close_time"])
        or series["fee_type"] != "quadratic"
        or (
            not optional_event_profile
            and (
                "fee_type_override" not in event
                or "fee_multiplier_override" not in event
                or event["fee_type_override"] is not None
                or event.get("fee_multiplier_override") is not None
            )
        )
    ):
        raise ValueError("FEE_CATALOG_OR_OVERRIDE_INVALID")
    price, floor = _decimal(request["price"]), _decimal(request["simulator_floor"])
    fees = single_buy_fees(price, _decimal(series["fee_multiplier"]), _decimal(policy.taker_rate))
    return {
        **request,
        **optional_fields,
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
