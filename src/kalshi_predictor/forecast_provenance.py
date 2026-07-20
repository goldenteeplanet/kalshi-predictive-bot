"""Immutable forecast-to-ranking provenance envelopes and synthetic audit."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kalshi_predictor.utils.time import utc_now


@dataclass(frozen=True)
class RankingProvenance:
    ticker: str
    forecast_id: str
    forecast_generated_at: str
    observation_id: str
    observation_timestamp: str
    feature_set_id: str
    feature_generated_at: str
    model_name: str
    model_version: str
    orderbook_snapshot_id: str
    orderbook_timestamp: str
    ranking_generated_at: str
    previous_digest: str = "GENESIS"

    def digest(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def envelope(self) -> dict[str, Any]:
        return {"attribution": asdict(self), "digest": self.digest(), "immutable": True}


def verify_envelope(envelope: dict[str, Any]) -> bool:
    try:
        attribution = RankingProvenance(**envelope["attribution"])
    except (KeyError, TypeError):
        return False
    return envelope.get("immutable") is True and envelope.get("digest") == attribution.digest()


def write_synthetic_provenance_audit(output_dir: Path) -> Path:
    records = []
    previous = "GENESIS"
    for category, ticker in (("crypto", "SYN-BTC"), ("weather", "SYN-NYC-WEATHER"),
                             ("sports", "SYN-SPORTS")):
        record = RankingProvenance(
            ticker=ticker, forecast_id=f"forecast:{ticker}:001",
            forecast_generated_at="2026-01-01T00:00:02+00:00",
            observation_id=f"observation:{category}:001",
            observation_timestamp="2026-01-01T00:00:00+00:00",
            feature_set_id=f"features:{ticker}:v1",
            feature_generated_at="2026-01-01T00:00:01+00:00",
            model_name=f"{category}_synthetic", model_version="1.0.0",
            orderbook_snapshot_id=f"book:{ticker}:3",
            orderbook_timestamp="2026-01-01T00:00:03+00:00",
            ranking_generated_at="2026-01-01T00:00:04+00:00",
            previous_digest=previous,
        )
        envelope = record.envelope()
        records.append(envelope)
        previous = envelope["digest"]
    report = {
        "phase": "FORECAST-PROVENANCE-1", "generated_at": utc_now().isoformat(),
        "mode": "LOCAL_SYNTHETIC_IMMUTABLE_ATTRIBUTION",
        "database_writes": 0, "execution_enabled": False,
        "records": records,
        "summary": {"records": len(records),
                    "all_digests_valid": all(verify_envelope(row) for row in records),
                    "chain_valid": all(
                        row["attribution"]["previous_digest"] ==
                        ("GENESIS" if index == 0 else records[index - 1]["digest"])
                        for index, row in enumerate(records)
                    )},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "forecast_ranking_provenance_audit.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path
