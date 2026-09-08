"""Pure, candidate-scoped PA6/PA8 qualification; no persistence or exchange calls.

Collector verdicts are internal validator outputs, never untrusted market JSON flags.
Hash integrity proves which artifact was considered, not the truth of its contents.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from kalshi_predictor.advanced_risk.engine import AdvancedRiskAction, AdvancedRiskDecision
from kalshi_predictor.overnight_paper.boundary import ExecutionMode
from kalshi_predictor.overnight_paper.source_health import aware, classify_source
from kalshi_predictor.position_sizing.sizer import PositionSizingDecision


class Readiness(StrEnum):
    PAPER_NOT_READY = "PAPER_NOT_READY"
    PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
    PAPER_READY_FOR_ACTIVATION = "PAPER_READY_FOR_ACTIVATION"


GATE_NAMES = (
    "CURRENT_PUBLIC_MARKET",
    "EXACT_IDENTITY",
    "CERTIFIED_SETTLEMENT_RULE",
    "FRESH_ANALYTICAL_SOURCE",
    "VALID_EXECUTABLE_BOOK",
    "POSITIVE_NET_EV",
    "PHASE_3M_NONZERO",
    "PHASE_3N_ALLOW",
    "FULL_PROVENANCE",
    "IDEMPOTENT_LOCAL_DECISION",
    "LOCAL_PAPER_MODE",
    "NO_EXCHANGE_PATH",
)
COLLECTOR_GATES = frozenset((1, 2, 3, 4, 5, 9, 12))
SEMANTIC_VERIFIERS = {
    1: "kalshi-public-market-v1",
    2: "kalshi-exact-catalog-identity-v1",
    4: "nws-hourly-both-clocks-v1",
    5: "kalshi-canonical-book-v1",
}
PUBLIC_BASE = "https://external-api.kalshi.com/trade-api/v2"


@dataclass(frozen=True)
class EvidenceReference:
    artifact: str
    sha256: str
    payload: bytes

    def valid(self) -> bool:
        return (
            bool(self.artifact and self.payload)
            and hashlib.sha256(self.payload).hexdigest() == self.sha256
        )


@dataclass(frozen=True)
class GateEvidence:
    """Internal semantic validator result bound to the full immutable decision.

    Only audited collector adapters may construct passing evidence. Raw public
    responses (even correctly hashed) are not validator reports. Persist the
    validator version and its underlying public response references in payload.
    """

    gate: int
    decision_id: str
    category: str
    ticker: str
    verifier: str
    reference: EvidenceReference
    blockers: tuple[str, ...] = ()
    sources: tuple[EvidenceReference, ...] = ()

    def verified(
        self,
        decision_inputs: dict[str, object] | None = None,
        *,
        as_of: datetime | None = None,
    ) -> bool:
        if not self.reference.valid() or not self.verifier or self.blockers:
            return False
        try:
            report = json.loads(self.reference.payload)
        except (ValueError, UnicodeError):
            return False
        structurally_valid = isinstance(report, dict) and all(
            (
                report.get("schema") == "overnight-paper-gate-v1",
                report.get("gate") == self.gate,
                report.get("decision_id") == self.decision_id,
                report.get("ticker") == self.ticker,
                report.get("category") == self.category,
                report.get("verifier") == self.verifier,
                report.get("verdict") == "PASS",
                bool(self.sources) and all(source.valid() for source in self.sources),
                report.get("sources") == [source.sha256 for source in self.sources],
            )
        )
        if not structurally_valid or decision_inputs is None:
            return False
        try:
            return self._semantically_verified(report, decision_inputs, as_of)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            return False

    def _semantically_verified(self, report: dict, inputs: dict, as_of: datetime | None) -> bool:
        # Gates without an audited semantic implementation cannot be manufactured
        # by choosing a verifier name or filling an attestation with PASS strings.
        # Rule/model/no-exchange certification remain unavailable in this release.
        if SEMANTIC_VERIFIERS.get(self.gate) != self.verifier:
            return False
        if decision_fingerprint(inputs) != self.decision_id:
            return False
        bound = inputs.get("source_hashes")
        if not isinstance(bound, list) or any(
            source.sha256 not in bound for source in self.sources
        ):
            return False
        decision_at = aware(inputs.get("decision_at"))
        at = aware(as_of) if as_of is not None else decision_at
        if at < decision_at or not aware(report["validated_at"]) <= at < aware(
            report["valid_until"]
        ):
            return False
        envelopes = [json.loads(source.payload) for source in self.sources]
        if any(not isinstance(item, dict) for item in envelopes):
            return False
        # Source receipts must be visible before the frozen decision. Acquisition
        # clocks never replace clocks embedded in the provider's actual payload.
        if any(
            not 0 <= (at - aware(item["received_at"])).total_seconds() <= 60
            or aware(item["received_at"]) > decision_at
            for item in envelopes
        ):
            return False
        by_url = {item["url"]: item["body"] for item in envelopes}
        if len(by_url) != len(envelopes):
            return False
        market_url = f"{PUBLIC_BASE}/markets/{self.ticker}"
        market = by_url[market_url]["market"]
        if market.get("ticker") != self.ticker or inputs.get("ticker") != self.ticker:
            return False
        if market.get("event_ticker") != inputs.get("event_id"):
            return False
        if inputs.get("category") != self.category:
            return False
        if market.get("status") not in {"open", "active"} or aware(market["close_time"]) <= at:
            return False
        if self.gate == 1:
            return True
        if self.gate == 2:
            event = by_url[f"{PUBLIC_BASE}/events/{inputs['event_id']}"]["event"]
            series = by_url[f"{PUBLIC_BASE}/series/{inputs['series']}"]["series"]
            return all(
                (
                    event.get("event_ticker") == inputs["event_id"],
                    event.get("series_ticker") == series.get("ticker") == inputs["series"],
                    series.get("category") == self.category,
                    isinstance(market.get("rules_primary"), str) and bool(market["rules_primary"]),
                    inputs.get("market_rules_hash")
                    == decision_fingerprint(
                        {
                            "primary": market.get("rules_primary"),
                            "secondary": market.get("rules_secondary"),
                            "contract_terms_url": series.get("contract_terms_url"),
                        }
                    ),
                )
            )
        if self.gate == 4:
            return self._fresh_nws_source(by_url, inputs, at, market)
        if self.gate == 5:
            return self._executable_book(by_url, envelopes, inputs, at, market)
        return False

    def _fresh_nws_source(self, by_url: dict, inputs: dict, at, market: dict) -> bool:
        if self.category != "Climate and Weather":
            return False
        # The only independently reviewed station/grid chain in this release.
        if (
            inputs.get("station") != "KNYC"
            or "(for coordinates KNYC)" not in market["rules_primary"]
        ):
            return False
        station = by_url["https://api.weather.gov/stations/KNYC"]
        if station["properties"]["stationIdentifier"] != "KNYC":
            return False
        lon, lat = station["geometry"]["coordinates"][:2]
        points = by_url[f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"]
        hourly_url = points["properties"]["forecastHourly"]
        if not re.fullmatch(
            r"https://api\.weather\.gov/gridpoints/OKX/\d+,\d+/forecast/hourly", hourly_url
        ):
            return False
        properties = by_url[hourly_url]["properties"]
        target = aware(market.get("occurrence_datetime") or market["close_time"])
        # Exact contract observation instant must fall inside a provider period.
        # Check the following second to preserve half-open end semantics.
        return any(
            isinstance(period.get("temperature"), int | float)
            and not isinstance(period.get("temperature"), bool)
            and math.isfinite(period["temperature"])
            and period.get("temperatureUnit") in {"F", "C"}
            and all(
                classify_source(
                    generated_at=properties.get("generatedAt"),
                    updated_at=properties.get("updateTime"),
                    valid_from=period.get("startTime"),
                    valid_to=period.get("endTime"),
                    target_start=target,
                    target_end=target + timedelta(seconds=1),
                    now=reference,
                    payload_hash=decision_fingerprint(properties),
                ).eligible
                for reference in (aware(inputs["decision_at"]), at)
            )
            for period in properties.get("periods", [])
        )

    def _executable_book(
        self, by_url: dict, envelopes: list, inputs: dict, at, market: dict
    ) -> bool:
        from kalshi_predictor.opportunities.scanner import top5_orderbook_notional
        from kalshi_predictor.opportunities.scoring import score_liquidity
        from kalshi_predictor.overnight_paper.books import qualify_book

        config = inputs.get("settings")
        if not isinstance(config, dict) or decision_fingerprint(config) != inputs.get(
            "config_hash"
        ):
            return False
        url = f"{PUBLIC_BASE}/markets/{self.ticker}/orderbook"
        book = by_url[url]
        receipt = next(item["received_at"] for item in envelopes if item["url"] == url)
        depth = top5_orderbook_notional(ticker=self.ticker, raw_orderbook_json=json.dumps(book))
        liquidity = score_liquidity(
            volume=market.get("volume_fp"),
            open_interest=market.get("open_interest_fp"),
            liquidity=max(Decimal(str(market.get("liquidity_dollars") or 0)), depth),
        )
        checked = qualify_book(
            book,
            received_at=aware(receipt),
            now=at,
            max_spread=Decimal(str(config["opportunity_max_spread"])),
            liquidity_score=liquidity,
            price_ranges=market.get("price_ranges"),
        )
        side = {"BUY_YES": "YES", "BUY_NO": "NO"}.get(str(inputs.get("side")))
        return (
            side is not None
            and checked["sides"][side]["executable"]
            and (Decimal(checked["sides"][side]["ask"]) == Decimal(str(inputs["executable_price"])))
        )


@dataclass(frozen=True)
class NetEV:
    model_probability: Decimal
    executable_price: Decimal
    gross_edge: Decimal
    estimated_fee: Decimal
    slippage_allowance: Decimal
    uncertainty_buffer: Decimal
    net_ev: Decimal


def compute_net_ev(
    *,
    model_probability: Decimal,
    executable_price: Decimal,
    estimated_fee: Decimal,
    slippage_allowance: Decimal,
    uncertainty_buffer: Decimal,
) -> NetEV:
    values = (
        model_probability,
        executable_price,
        estimated_fee,
        slippage_allowance,
        uncertainty_buffer,
    )
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ValueError("EV inputs must be finite Decimal values")
    if not 0 <= model_probability <= 1 or not 0 < executable_price < 1:
        raise ValueError("Probability or executable price outside binary contract range")
    if any(value < 0 for value in values[2:]):
        raise ValueError("Costs and uncertainty cannot be negative")
    gross = model_probability - executable_price
    return NetEV(
        model_probability,
        executable_price,
        gross,
        estimated_fee,
        slippage_allowance,
        uncertainty_buffer,
        gross - estimated_fee - slippage_allowance - uncertainty_buffer,
    )


def decision_fingerprint(inputs: dict[str, object]) -> str:
    """Caller includes forecast, snapshot, rules, clocks, engine/config versions.

    JSON serialization rejects NaN and does not coerce arbitrary objects to strings.
    The writer must enforce a UNIQUE key and compare the complete input payload.
    """
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class CandidateQualification:
    status: Readiness
    decision_id: str
    gates: tuple[tuple[str, bool], ...]
    blockers: tuple[str, ...]
    net_ev: Decimal | None


def qualify_candidate(
    *,
    ticker: str,
    category: str,
    decision_inputs: dict[str, object],
    decision_id: str,
    evidence: tuple[GateEvidence, ...] = (),
    ev: NetEV | None = None,
    minimum_net_ev: Decimal,
    phase3m: PositionSizingDecision | None = None,
    phase3n: AdvancedRiskDecision | None = None,
    mode: ExecutionMode = ExecutionMode.OBSERVATION_ONLY,
    existing_decision_ids: frozenset[str] = frozenset(),
) -> CandidateQualification:
    """Authorization, atomic reconciliation and exact-SHA checks remain writer gates.

    minimum_net_ev must come from unchanged Settings.paper_min_edge. No function
    here enables flags or returns READY_FOR_ACTIVATION from qualification alone.
    """
    if not minimum_net_ev.is_finite() or minimum_net_ev < 0:
        raise ValueError("Invalid configured minimum net EV")
    expected_id = decision_fingerprint(decision_inputs)
    flags: dict[int, bool] = {}
    reasons: list[str] = []
    for gate in sorted(COLLECTOR_GATES):
        matching = [
            item
            for item in evidence
            if item.gate == gate and item.ticker == ticker and item.category == category
        ]
        flags[gate] = len(matching) == 1 and all(
            (
                matching[0].decision_id == expected_id,
                matching[0].ticker == ticker,
                matching[0].category == category,
                matching[0].verified(decision_inputs),
            )
        )
        for item in matching:
            reasons.extend(item.blockers)
    recalculated = (
        None
        if ev is None
        else compute_net_ev(
            model_probability=ev.model_probability,
            executable_price=ev.executable_price,
            estimated_fee=ev.estimated_fee,
            slippage_allowance=ev.slippage_allowance,
            uncertainty_buffer=ev.uncertainty_buffer,
        )
    )
    flags[6] = ev is not None and ev == recalculated and ev.net_ev > minimum_net_ev
    flags[7] = phase3m is not None and all(
        (
            phase3m.version == "3M",
            phase3m.proposed_contracts > 0,
            decision_inputs.get("phase3m_hash") == decision_fingerprint(phase3m.as_dict()),
            phase3m.live_candidate_contracts > 0,
            not any(code in phase3m.reason_codes for code in ("INVALID_INPUT", "HARD_RISK_BLOCK")),
        )
    )
    flags[8] = (
        phase3n is not None
        and phase3m is not None
        and all(
            (
                phase3n.version == "3N",
                phase3n.action == AdvancedRiskAction.ALLOW,
                decision_inputs.get("phase3n_hash") == decision_fingerprint(phase3n.as_dict()),
                phase3n.live_candidate_contracts > 0,
                not phase3n.hard_blocks,
                phase3n.phase_3m_proposed_contracts == phase3m.proposed_contracts,
            )
        )
    )
    flags[10] = bool(ticker and category and decision_inputs) and all(
        (
            decision_id == expected_id,
            decision_id not in existing_decision_ids,
            decision_inputs.get("ticker") == ticker,
            decision_inputs.get("category") == category,
            all(
                decision_inputs.get(key)
                for key in (
                    "event_id",
                    "series",
                    "forecast_id",
                    "snapshot_id",
                    "rule_version",
                    "source_hashes",
                    "model_version",
                    "config_hash",
                    "phase3m_hash",
                    "phase3n_hash",
                )
            ),
        )
    )
    flags[11] = mode == ExecutionMode.LOCAL_PAPER
    # Gate 12 requires the audited writer artifact, not merely this clean evaluator.
    gates = tuple((name, flags[index]) for index, name in enumerate(GATE_NAMES, 1))
    reasons.extend(name for name, passed in gates if not passed)
    return CandidateQualification(
        Readiness.PAPER_ELIGIBLE if all(flags.values()) else Readiness.PAPER_NOT_READY,
        decision_id,
        gates,
        tuple(dict.fromkeys(reasons)),
        None if ev is None else ev.net_ev,
    )
