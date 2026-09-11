"""Audited gate-four Miami original/replay binding; no other gate authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from kalshi_predictor.overnight_paper.miami_binding import (
    MiamiOriginal,
    _at,
    _decode,
    bind_miami_forecast_to_contract,
)
from kalshi_predictor.overnight_paper.miami_source import _receipt, verify_miami_source
from kalshi_predictor.overnight_paper.provenance import Artifact, canonical_hash

VERIFIER = "miami-canonical-original-replay-v1"
SOURCE_KIND = "miami-canonical-index-v1"
# Reviewed frozen computation closure. These hashes do not certify model skill.
REPLAY_CODE_HASHES = {
    "scripts/positive_ev_miami_research.py": (
        "6f10e36c4b01a0eb141494116f785767c6cb6918464d3fd3b0ef27496a87b12e"
    ),
    "src/kalshi_predictor/weather/miami_forecast.py": (
        "a7ee152f3de684e9fa0ef287995a3ce45af15e4d41783a80a4c553b30aac29b3"
    ),
    "src/kalshi_predictor/weather/miami_index.py": (
        "d03e6713979efdebf186f51e985ba1b7caaf671524e612ed5fb2b5b21c545b7f"
    ),
    "src/kalshi_predictor/crypto/research_provenance.py": (
        "d60a2f0c230ff3bd139e43d8d2f479e8d769d7f899206dcaac501a2481763123"
    ),
}


# Separate reviewed research closure; hourly pins above remain unchanged.
GRID30_REPLAY_CODE_HASHES = {
    path: sha
    for path, sha in REPLAY_CODE_HASHES.items()
    if path != "scripts/positive_ev_miami_research.py"
} | {
    "scripts/positive_ev_miami_half_hour_research.py": (
        "4ca8e8abc4efca6891c9bf48effe31f060b90598196899b94c68e6af4b28aa6c"
    ),
    "src/kalshi_predictor/weather/miami_half_hour_forecast.py": (
        "8b00b7315069ea965eacf004436a6c03f40b3ef60d14ba65cc392348902d5664"
    ),
}

# Reviewed Git/Linux closure. Original Windows bytes remain a distinct profile;
# no runtime normalization or per-file mixing can grant source authority.
GRID30_LF_REPLAY_CODE_HASHES = GRID30_REPLAY_CODE_HASHES | {
    "scripts/positive_ev_miami_half_hour_research.py": (
        "86f66fd98458abd6a47968e9e2ccbf6827f4c2d3c46d74ddfd481bedee692eb7"
    ),
    "src/kalshi_predictor/weather/miami_half_hour_forecast.py": (
        "175aa435b60911cf3223fb69be1f0fae495fd8c715c17279e12c3114533e2eb7"
    ),
}


@dataclass(frozen=True)
class MiamiCaptureEvidence:
    index: MiamiOriginal
    calibrations: MiamiOriginal
    index_receipt: Artifact
    calibrations_receipt: Artifact


@dataclass(frozen=True)
class MiamiGateContext:
    frozen_prediction: Artifact
    recording_receipt: Artifact
    captures: tuple[MiamiCaptureEvidence, ...]
    current_capture: int
    market: MiamiOriginal
    event: MiamiOriginal
    series: MiamiOriginal
    catalog_receipts: tuple[Artifact, Artifact, Artifact]
    rule_documents: tuple[Artifact, ...]
    code_originals: tuple[tuple[str, Artifact], ...]

    def artifacts(self) -> tuple[Artifact, ...]:
        originals = [
            self.frozen_prediction,
            self.recording_receipt,
            self.market.artifact,
            self.event.artifact,
            self.series.artifact,
            *self.catalog_receipts,
            *self.rule_documents,
        ]
        for pair in self.captures:
            originals.extend(
                (
                    pair.index.artifact,
                    pair.calibrations.artifact,
                    pair.index_receipt,
                    pair.calibrations_receipt,
                )
            )
        originals.extend(artifact for _, artifact in self.code_originals)
        return tuple(originals)

    def fingerprint(self) -> str:
        def original(value: MiamiOriginal) -> dict:
            return {
                "sha256": value.artifact.sha256,
                "url": value.url,
                "received_at": _at(value.received_at).isoformat(),
            }

        return canonical_hash(
            {
                "artifacts": [a.sha256 for a in self.artifacts()],
                "captures": [[original(c.index), original(c.calibrations)] for c in self.captures],
                "current_capture": self.current_capture,
                "catalog": [original(o) for o in (self.market, self.event, self.series)],
                "code_paths": [path for path, _ in self.code_originals],
                "historical_public_availability": "UNKNOWN",
            }
        )


def verify_miami_gate4(
    context: MiamiGateContext,
    *,
    inputs: dict,
    sources: tuple[tuple[str, bytes], ...],
    now: datetime,
) -> bool:
    """Recompute source truth; typed context and PASS strings alone are insufficient."""
    model = inputs.get("model_name")
    code_profiles: tuple[dict[str, str], ...]
    if model == "miami_prior_day_increment_v1":
        code_profiles, origin_grid = (REPLAY_CODE_HASHES,), 60
    elif model == "miami_prior_day_increment_grid30_v1":
        code_profiles, origin_grid = (
            GRID30_REPLAY_CODE_HASHES, GRID30_LF_REPLAY_CODE_HASHES
        ), 30
    else:
        return False
    if (
        type(context.current_capture) is not int
        or not 1 <= len(context.captures) <= 12
        or not 0 <= context.current_capture < len(context.captures)
        or inputs.get("category") != "Climate and Weather"
        or inputs.get("series") != "KXTEMPMIAH"
        or inputs.get("source_kind") != SOURCE_KIND
        or inputs.get("model_version") != "1"
        or inputs.get("historical_public_availability") != "UNKNOWN"
        or inputs.get("miami_context_sha256") != context.fingerprint()
    ):
        return False
    artifacts = context.artifacts()
    expected = {(a.sha256, a.payload) for a in artifacts}
    if set(sources) != expected or len(sources) != len(expected):
        return False
    bound_hashes = inputs.get("source_hashes")
    if not isinstance(bound_hashes, list) or any(sha not in bound_hashes for sha, _ in sources):
        return False
    # All exact original bytes are checked, including non-JSON code/rules.
    import hashlib

    if any(hashlib.sha256(a.payload).hexdigest() != a.sha256 for a in artifacts):
        return False
    code_hashes = dict((path, a.sha256) for path, a in context.code_originals)
    if code_hashes not in code_profiles:
        return False
    if len(context.code_originals) != len(code_hashes):
        return False
    saved = _decode(context.frozen_prediction)
    forecasts = saved["prediction"]["forecasts"]
    if not forecasts or any(f.get("model") != model for f in forecasts):
        return False
    proof = saved["prediction"]["code_proof"]
    if (
        {item["path"]: item["sha256"] for item in proof["files"]} != code_hashes
        or len(proof["files"]) != len(code_hashes)
        or _at(proof["commit_recorded_at"]) != _at(saved["model_committed_at"])
        or not _at(saved["model_committed_at"])
        <= _at(proof["code_frozen_at"])
        <= _at(saved["model_input_as_of"])
    ):
        return False
    decision, current = _at(inputs["decision_at"]), _at(now)
    if current < decision:
        return False
    for original, receipt in zip(
        (context.market, context.event, context.series),
        context.catalog_receipts,
        strict=True,
    ):
        _receipt(original, receipt, original.url)
        if (
            not _at(original.received_at) <= decision
            or not 0 <= (current - _at(original.received_at)).total_seconds() <= 60
        ):
            return False
    for pair in context.captures:
        _receipt(pair.index, pair.index_receipt, pair.index.url)
        _receipt(pair.calibrations, pair.calibrations_receipt, pair.calibrations.url)
    bound = bind_miami_forecast_to_contract(
        frozen_prediction=context.frozen_prediction,
        recording_receipt=context.recording_receipt,
        captures=tuple((c.index, c.calibrations) for c in context.captures),
        market=context.market,
        event=context.event,
        series=context.series,
        rule_documents=context.rule_documents,
        now=decision,
    )
    if (
        inputs.get("ticker") != bound.ticker
        or inputs.get("event_id") != bound.event_ticker
        or inputs.get("frozen_prediction_sha256") != context.frozen_prediction.sha256
        or _at(inputs["observation_time"]) != bound.target_at
        or _at(inputs["model_input_as_of"]) != _at(saved["model_input_as_of"])
        or Decimal(str(inputs["forecast_probability"])) != Decimal(str(bound.probability_yes))
    ):
        return False
    forecast = next(
        f for f in saved["prediction"]["forecasts"] if _at(f["target_at"]) == bound.target_at
    )
    if _at(inputs["origin_at"]) != _at(forecast["origin_at"]):
        return False
    market = _decode(context.market.artifact)["market"]
    series = _decode(context.series.artifact)["series"]
    if (
        market.get("status") not in {"open", "active"}
        or _at(market["close_time"]) <= current
        or series.get("category") != inputs["category"]
    ):
        return False
    pair = context.captures[context.current_capture]
    health = verify_miami_source(
        index=pair.index,
        calibrations=pair.calibrations,
        index_receipt=pair.index_receipt,
        calibrations_receipt=pair.calibrations_receipt,
        origin_at=_at(inputs["origin_at"]),
        target_at=bound.target_at,
        model_input_as_of=_at(saved["model_input_as_of"]),
        decision_at=decision,
        now=current,
        origin_grid_minutes=origin_grid,
    )
    return health.source_healthy
