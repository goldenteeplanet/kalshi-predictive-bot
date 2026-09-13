"""Bounded explicit dependence metadata; no effective-N or calibration authority."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime


def dependency_graph(rows: tuple[dict, ...]) -> dict:
    """Known sharing forms components; asset membership alone is a risk label.

    All inputs are declaration metadata, not original-custody proof. No outcome
    or score is an input. Closed source windows sharing an endpoint are joined.
    """
    if type(rows) is not tuple or len(rows) > 512:
        raise ValueError("DEPENDENCY_ROW_BOUND")
    fields = {"decision_id", "ticker", "event", "asset", "settlement_event", "target_at",
              "source_start", "source_end", "recorded_at", "input_hashes", "role"}
    parsed = []
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != fields:
            raise ValueError("DEPENDENCY_FIELDS")
        for key in fields - {"input_hashes"}:
            if type(row[key]) is not str or not 0 < len(row[key]) <= 160:
                raise ValueError("DEPENDENCY_TEXT_BOUND")
        if re.fullmatch(r"[a-f0-9]{64}", row["decision_id"]) is None:
            raise ValueError("DEPENDENCY_DECISION_HASH")
        if row["decision_id"] in seen:
            raise ValueError("DEPENDENCY_DUPLICATE_DECISION")
        seen.add(row["decision_id"])
        if row["asset"] not in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
            raise ValueError("DEPENDENCY_ASSET")
        if row["role"] not in {"DEVELOPMENT", "FUTURE_HOLDOUT", "HYPOTHESIS_ONLY"}:
            raise ValueError("DEPENDENCY_ROLE")
        hashes = row["input_hashes"]
        if (type(hashes) is not tuple or not 1 <= len(hashes) <= 32
                or any(type(h) is not str or re.fullmatch(r"[a-f0-9]{64}", h) is None
                       for h in hashes) or len(set(hashes)) != len(hashes)):
            raise ValueError("DEPENDENCY_INPUT_HASHES")
        clocks = {}
        for key in ("source_start", "source_end", "recorded_at", "target_at"):
            stamp = datetime.fromisoformat(row[key])
            if stamp.utcoffset() is None:
                raise ValueError("DEPENDENCY_AWARE_CLOCK")
            clocks[key] = stamp.astimezone(UTC)
        if not (
            clocks["source_start"] <= clocks["source_end"]
            <= clocks["recorded_at"] < clocks["target_at"]
        ):
            raise ValueError("DEPENDENCY_PRETARGET_CHRONOLOGY")
        parsed.append((row, clocks))
    parsed.sort(key=lambda item: item[0]["decision_id"])
    parent = list(range(len(parsed)))

    def root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    edges = []
    asset_members: dict[str, list[str]] = {}
    for index, (row, clock) in enumerate(parsed):
        asset_members.setdefault(row["asset"], []).append(row["decision_id"])
        for other_index in range(index):
            other, other_clock = parsed[other_index]
            reasons = []
            for key in ("ticker", "event", "settlement_event"):
                if row[key] == other[key]:
                    reasons.append("SHARED_" + key.upper())
            if clock["target_at"] == other_clock["target_at"]:
                reasons.append("SAME_TARGET_TIME")
            if set(row["input_hashes"]) & set(other["input_hashes"]):
                reasons.append("SHARED_MODEL_INPUT_ORIGINAL")
            if (row["asset"] == other["asset"]
                    and max(clock["source_start"], other_clock["source_start"])
                    <= min(clock["source_end"], other_clock["source_end"])):
                reasons.append("SAME_ASSET_OVERLAPPING_CF_WINDOW")
            if reasons:
                parent[root(index)] = root(other_index)
                edges.append({"left": other["decision_id"], "right": row["decision_id"],
                              "reasons": reasons})
    groups: dict[int, list[dict]] = {}
    for index, (row, _) in enumerate(parsed):
        groups.setdefault(root(index), []).append(row)
    components = []
    for members in groups.values():
        ids = sorted(row["decision_id"] for row in members)
        roles = sorted({row["role"] for row in members})
        components.append({"component_id": hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
                           "members": ids, "roles": roles,
                           "cross_role_excluded": len(roles) > 1})
    return {"version": "DECLARED_DEPENDENCY_GRAPH_V1", "input_count": len(rows),
            "contract_n": len({r["ticker"] for r in rows}),
            "event_n": len({r["event"] for r in rows}), "dependency_group_n": len(components),
            "components": sorted(components, key=lambda row: row["component_id"]),
            "relationships": edges, "same_asset_potential_dependence": asset_members,
            "effective_independent_n": None, "original_custody_verified": False,
            "independence_established": False, "paper_eligible": False}
