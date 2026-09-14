"""Reviewed DOGE closed interval evidence. Local receipts are not external attestation."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from kalshi_predictor.crypto.doge_strikes import parse_doge_strike
from kalshi_predictor.microstructure.provenance import aware

TERMS_URL = "https://assets.kalshi.com/contract_terms/DOGE.pdf"
SERIES_URL = "https://external-api.kalshi.com/trade-api/v2/series/KXDOGE"
TERMS_SHA256 = "1cea2170e7b269f335bb76d31421e5bcefe94dc24084525444117fabc7bcd4e3"
PROFILE_AVAILABLE_AT = datetime(2026, 9, 11, 6, 30, 46, 541461, tzinfo=UTC)


def strict_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for k, v in items:
            if k in result:
                raise ValueError("DUPLICATE_ORIGINAL_KEY")
            result[k] = v
        return result

    def reject(value):
        raise ValueError("NONFINITE_ORIGINAL")

    def walk(value):
        if isinstance(value, float) and not math.isfinite(value):
            reject(value)
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    if type(raw) is not bytes or not 0 < len(raw) <= 1_000_000:
        raise ValueError("BOUNDED_ORIGINAL_REQUIRED")
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
    walk(value)
    if type(value) is not dict:
        raise ValueError("ORIGINAL_OBJECT_REQUIRED")
    return value


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class DogeOriginal:
    raw: bytes
    receipt_raw: bytes

    def validate(self, url: str, cutoff: datetime) -> dict:
        receipt = strict_json(self.receipt_raw)
        if (
            type(self.raw) is not bytes
            or not 0 < len(self.raw) <= 1_000_000
            or type(receipt.get("status")) is not int
            or receipt["status"] != 200
            or receipt.get("url") != url
            or receipt.get("sha256") != sha(self.raw)
        ):
            raise ValueError("DOGE_ORIGINAL_HASH_URL_STATUS")
        try:
            requested = aware(datetime.fromisoformat(receipt["requested_at"]))
            received = aware(datetime.fromisoformat(receipt["received_at"]))
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError("DOGE_RECEIPT_CLOCK_REQUIRED") from exc
        if not requested <= received <= aware(cutoff):
            raise ValueError("DOGE_ORIGINAL_CLOCK")
        return receipt


@dataclass(frozen=True)
class DogeRangeProof:
    series: DogeOriginal
    terms: DogeOriginal

    def validate(self, cutoff: datetime) -> dict:
        cutoff = aware(cutoff)
        if type(self.series) is not DogeOriginal or type(self.terms) is not DogeOriginal:
            raise ValueError("DOGE_ORIGINAL_PROOF_REQUIRED")
        sr = self.series.validate(SERIES_URL, cutoff)
        tr = self.terms.validate(TERMS_URL, cutoff)
        data = strict_json(self.series.raw).get("series")
        if (
            type(data) is not dict
            or data.get("ticker") != "KXDOGE"
            or data.get("contract_terms_url") != TERMS_URL
            or sha(self.terms.raw) != TERMS_SHA256
            or tr.get("series_original_sha256") != sha(self.series.raw)
        ):
            raise ValueError("DOGE_REVIEWED_TERMS_BINDING_REQUIRED")
        available = max(
            PROFILE_AVAILABLE_AT,
            aware(datetime.fromisoformat(sr["received_at"])),
            aware(datetime.fromisoformat(tr["received_at"])),
        )
        if available > cutoff:
            raise ValueError("DOGE_PROFILE_NOT_YET_AVAILABLE")
        return {
            "schema": "doge-closed-range-proof-v1",
            "profile": "DOGE_TERMS_CLOSED_CLOSED_V1",
            "series_sha256": sha(self.series.raw),
            "series_receipt_sha256": sha(self.series.receipt_raw),
            "terms_sha256": sha(self.terms.raw),
            "terms_receipt_sha256": sha(self.terms.receipt_raw),
            "available_at": available.isoformat(),
            "payout_interval": "CLOSED_CLOSED",
            "execution_authority": False,
            "settlement_reconstructed": False,
            "rounding_rule": None,
            "partition_normalized": False,
        }

    def bind_market(self, market: dict, original: DogeOriginal, cutoff: datetime) -> dict:
        evidence = self.validate(cutoff)
        parsed = parse_doge_strike(market, cutoff=cutoff)
        if type(original) is not DogeOriginal:
            raise ValueError("DOGE_ACTUAL_MARKET_ORIGINAL_REQUIRED")
        receipt = original.validate(
            "https://api.elections.kalshi.com/trade-api/v2/markets/" + parsed.ticker, cutoff
        )
        actual = strict_json(original.raw).get("market")
        if (
            type(actual) is not dict
            or json.dumps(actual, sort_keys=True, allow_nan=False)
            != json.dumps(market, sort_keys=True, allow_nan=False)
            or parsed.operator != "between"
        ):
            raise ValueError("DOGE_ORIGINAL_MARKET_BINDING_REQUIRED")
        evidence["available_at"] = max(
            aware(datetime.fromisoformat(evidence["available_at"])),
            aware(datetime.fromisoformat(receipt["received_at"])),
        ).isoformat()
        return dict(
            evidence,
            ticker=parsed.ticker,
            event_ticker=parsed.event_ticker,
            target_at=parsed.close_time.isoformat(),
            floor=str(parsed.floor),
            cap=str(parsed.cap),
            market_sha256=sha(original.raw),
            market_receipt_sha256=sha(original.receipt_raw),
            market_received_at=receipt["received_at"],
        )
