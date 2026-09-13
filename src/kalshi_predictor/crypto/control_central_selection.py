"""Pure event-metadata control plus two central strikes; no acquisition or admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Context, Decimal, localcontext
from typing import Any

from kalshi_predictor.crypto.cf_process_inputs import _json, _time, digest
from kalshi_predictor.crypto.cf_selection_level import (
    PURPOSE,
    CFSelectionLevel,
    replay_cf_selection_level,
)
from kalshi_predictor.crypto.current_event_selection import (
    SERIES,
    CurrentEventDiscovery,
    _geometry,
    _number,
    event_discovery_rows,
)
from kalshi_predictor.crypto.settlement_target import aware

POLICY = "EVENT_METADATA_CONTROL_PLUS_TWO_CENTRAL_V1"
SCHEMA = "control-central-selection-protocol-v1"


@dataclass(frozen=True)
class ControlCentralSelectionInputs:
    discovery: CurrentEventDiscovery
    level: CFSelectionLevel
    protocol_original: bytes
    protocol_sha256: str
    expected_source_commit: str
    selected_at: datetime


def select_control_and_central(
    inputs: ControlCentralSelectionInputs, *, asset: str, assessed_at: datetime
) -> dict[str, Any]:
    """Replay control metadata ranking and two central strikes without forecast EV.

    Hashes and local clocks bind supplied originals, not external attestation.
    The controller must preserve this result before acquiring forecast/book inputs.
    """
    if (
        type(inputs) is not ControlCentralSelectionInputs
        or type(inputs.discovery) is not CurrentEventDiscovery
        or type(inputs.level) is not CFSelectionLevel
        or type(asset) is not str
        or asset not in ("BTC", "SOL")
    ):
        raise ValueError("EXACT_LEVEL_EVENT_SELECTION_INPUTS_REQUIRED")
    aware(assessed_at)
    aware(inputs.selected_at)
    if inputs.selected_at > assessed_at:
        raise ValueError("LEVEL_EVENT_SELECTION_NOT_VISIBLE")
    raw = inputs.protocol_original
    if type(raw) is not bytes or not 0 < len(raw) <= 10_000:
        raise ValueError("LEVEL_EVENT_PROTOCOL_SIZE")
    plan = _json(raw, inputs.protocol_sha256)
    if set(plan) != {
        "schema",
        "asset",
        "event_ticker",
        "source_commit",
        "declared_at",
        "not_before",
        "not_after",
        "policy",
        "max_contracts",
        "purpose",
        "paper_eligible",
        "execution_authority",
    } or (
        plan["schema"] != SCHEMA
        or plan["asset"] != asset
        or plan["event_ticker"] != inputs.discovery.event_ticker
        or plan["source_commit"] != inputs.expected_source_commit
        or plan["policy"] != POLICY
        or type(plan["max_contracts"]) is not int
        or plan["max_contracts"] != 3
        or plan["purpose"] != PURPOSE
        or plan["paper_eligible"] is not False
        or plan["execution_authority"] is not False
    ):
        raise ValueError("LEVEL_EVENT_PREDECLARED_POLICY_REQUIRED")
    declared, begin, end = (_time(plan[k]) for k in ("declared_at", "not_before", "not_after"))
    if not declared <= begin <= inputs.selected_at <= assessed_at < end or not (
        0 < (end - begin).total_seconds() <= 180
    ):
        raise ValueError("LEVEL_EVENT_PROTOCOL_CLOCK")
    universe = event_discovery_rows(
        inputs.discovery, series=SERIES[asset], assessed_at=inputs.selected_at
    )
    level = replay_cf_selection_level(
        inputs.level,
        expected_asset=asset,
        expected_event_ticker=inputs.discovery.event_ticker,
        expected_source_commit=inputs.expected_source_commit,
        as_of=inputs.selected_at,
    )
    event_receipt = _json(inputs.discovery.receipt, digest(inputs.discovery.receipt))
    cf_receipt = _json(level.receipt_original, level.receipt_sha256)
    level_plan = _json(level.protocol_original, level.protocol_sha256)
    if not (
        declared <= _time(level_plan["declared_at"])
        and begin
        <= _time(event_receipt["requested_at"])
        <= inputs.discovery.original.received_at
        <= _time(cf_receipt["requested_at"])
        <= level.available_at
        <= inputs.selected_at
    ):
        raise ValueError("LEVEL_EVENT_ACQUISITION_CHRONOLOGY")
    # Current consumption cannot rely on the original selection's freshness alone.
    replay_cf_selection_level(
        inputs.level,
        expected_asset=asset,
        expected_event_ticker=inputs.discovery.event_ticker,
        expected_source_commit=inputs.expected_source_commit,
        as_of=assessed_at,
    )
    event_discovery_rows(inputs.discovery, series=SERIES[asset], assessed_at=assessed_at)
    candidates, exclusions = [], []
    for row in universe["rows"]:
        market = row["market"]
        try:
            with localcontext(Context(prec=80)):
                distance, center = _geometry(
                    market, level.level, asset=asset, as_of=inputs.selected_at
                )
        except ValueError as exc:
            exclusions.append({"ticker": market["ticker"], "reason": str(exc)})
            continue
        with localcontext(Context(prec=80)):
            try:
                bid = _number(market.get("yes_bid_dollars") or "0")
                ask = _number(market.get("yes_ask_dollars") or "1")
                if not 0 <= bid <= ask <= 1:
                    raise ValueError("CONTROL_METADATA_PRICE_INVALID")
                valid = True
                spread, midpoint = ask - bid, abs((ask + bid) / 2 - Decimal(".5"))
                two_sided = 0 < bid < ask < 1
            except ValueError:
                valid, two_sided = False, False
                spread, midpoint = Decimal(1), Decimal(1)
        candidates.append({"ticker": market["ticker"], "distance": str(distance),
            "center_distance": str(center), "metadata_valid": valid,
            "metadata_two_sided": two_sided, "metadata_spread": str(spread) if valid else None,
            "metadata_midpoint_distance": str(midpoint) if valid else None})
    candidates.sort(
        key=lambda row: (Decimal(row["distance"]), Decimal(row["center_distance"]), row["ticker"])
    )
    controls = [row for row in candidates if row["metadata_valid"]]
    controls.sort(key=lambda row: (not row["metadata_two_sided"],
        Decimal(row["metadata_spread"]), Decimal(row["metadata_midpoint_distance"]), row["ticker"]))
    roles = {"EVENT_METADATA_CONTROL": controls[0]["ticker"] if controls else None,
             "CENTRAL_1": candidates[0]["ticker"] if candidates else None,
             "CENTRAL_2": candidates[1]["ticker"] if len(candidates) > 1 else None}
    selected = list(dict.fromkeys(ticker for ticker in roles.values() if ticker is not None))
    manifest = {
        "schema": "control-central-selection-manifest-v1",
        "purpose": PURPOSE,
        "policy": POLICY,
        "selected_at": inputs.selected_at.isoformat(),
        "protocol_original_json": raw.decode(),
        "protocol_sha256": inputs.protocol_sha256,
        "event_original_json": inputs.discovery.original.payload.decode(),
        "event_receipt_original_json": inputs.discovery.receipt.decode(),
        "event_sha256": inputs.discovery.original.sha256,
        "event_receipt_sha256": digest(inputs.discovery.receipt),
        "cf_level_original_json": level.body_original.decode(),
        "cf_level_receipt_original_json": level.receipt_original.decode(),
        "cf_level_protocol_original_json": level.protocol_original.decode(),
        "cf_level_sha256": level.source_sha256,
        "cf_level_receipt_sha256": level.receipt_sha256,
        "cf_level_protocol_sha256": level.protocol_sha256,
        "benchmark_level": str(level.level),
        "benchmark_observed_at": level.observed_at.isoformat(),
        "source_commit_binding": inputs.expected_source_commit,
        "source_commit_attested": False,
        "universe_count": len(universe["rows"]),
        "scores": candidates,
        "exclusions": sorted(exclusions, key=lambda row: row["ticker"]),
        "selected": selected,
        "roles": roles,
        "control_scope": "SAME_COMPLETE_EVENT_NOT_FROZEN_PAGINATION_CONTROL",
        "preselection_budget": {"event_rows": 200, "cf_level_gets": 1,
                                "contracts": 3, "side_assessments": 6, "books": 3},
        "execution_budget_enforced": False,
        "dependencies": {"same_event": inputs.discovery.event_ticker,
                         "shared_cf_original": level.source_sha256, "independent_n": None},
        "book_selection_frozen": True,
        "liquidity_verified": False,
        "forecast_cf_requires_new_target_bound_request": True,
        "level_is_forecast_input": False,
        "paper_eligible": False,
        "execution_authority": False,
        "external_timestamp_attestation": False,
    }
    return {
        **universe,
        "rows": [row for row in universe["rows"] if row["market"]["ticker"] in selected],
        "selection_manifest": manifest,
        "selection_universe_count": universe["unique_markets"],
        "assessment_scope": "PREDECLARED_CONTROL_AND_TWO_CENTRAL_OF_COMPLETE_EVENT",
        "selected_markets": len(selected),
    }
