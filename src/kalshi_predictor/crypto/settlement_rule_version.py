"""Exact semantic identities for prospective forecasts; hashes do not certify rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime


@dataclass(frozen=True)
class CryptoSettlementRuleVersion:
    family: str
    benchmark_id: str
    sample_frequency_ms: int | None
    sample_count: int | None
    start_offset_ms: int | None
    end_offset_ms: int | None
    include_start: bool | None
    include_end: bool | None
    sample_precision: str | None
    sample_rounding: str | None
    average_precision: str | None
    final_precision: str | None
    final_rounding: str | None
    tie_breaking: str | None
    missing_sample_behavior: str | None
    amendment_handling: str | None
    finality: str | None
    effective_from: str | None
    effective_until: str | None
    authority_version: str | None
    # Exact field name -> original document hash. No inferred default semantics.
    field_evidence: tuple[tuple[str, str], ...]
    conflicting_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.family or not self.benchmark_id:
            raise ValueError("EXACT_FAMILY_AND_BENCHMARK_REQUIRED")
        numeric = (self.sample_frequency_ms, self.sample_count)
        if any(v is not None and (type(v) is not int or v <= 0) for v in numeric):
            raise ValueError("POSITIVE_SAMPLE_GRID_REQUIRED")
        for v in (self.start_offset_ms, self.end_offset_ms):
            if v is not None and type(v) is not int:
                raise ValueError("INTEGER_WINDOW_OFFSETS_REQUIRED")
        for v in (self.include_start, self.include_end):
            if v is not None and type(v) is not bool:
                raise ValueError("EXPLICIT_ENDPOINT_FLAGS_REQUIRED")
        if self.start_offset_ms is not None and self.end_offset_ms is not None:
            if self.start_offset_ms >= self.end_offset_ms:
                raise ValueError("ORDERED_WINDOW_REQUIRED")
            if all(v is not None for v in (*numeric, self.include_start, self.include_end)):
                assert self.sample_frequency_ms is not None
                span = self.end_offset_ms - self.start_offset_ms
                count = span // self.sample_frequency_ms - 1
                count += int(bool(self.include_start)) + int(bool(self.include_end))
                if span % self.sample_frequency_ms or count != self.sample_count:
                    raise ValueError("SAMPLE_COUNT_DISAGREES_WITH_ENDPOINTS")
        for text in (self.effective_from, self.effective_until):
            if text is not None:
                clock = datetime.fromisoformat(text)
                if clock.utcoffset() is None:
                    raise ValueError("AWARE_EFFECTIVE_TIME_REQUIRED")
        if self.effective_from and self.effective_until:
            if datetime.fromisoformat(self.effective_from) >= datetime.fromisoformat(
                self.effective_until
            ):
                raise ValueError("ORDERED_EFFECTIVE_INTERVAL_REQUIRED")
        fields = set(asdict(self)) - {"field_evidence", "conflicting_fields"}
        names = [name for name, _ in self.field_evidence]
        if len(names) != len(set(names)) or not set(names) <= fields:
            raise ValueError("UNIQUE_SEMANTIC_FIELD_EVIDENCE_REQUIRED")
        if not set(self.conflicting_fields) <= fields:
            raise ValueError("UNKNOWN_CONFLICT_FIELD")
        for _, digest in self.field_evidence:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("ORIGINAL_SHA256_REQUIRED")

    @property
    def version_id(self) -> str:
        value = asdict(self)
        value["field_evidence"] = sorted(self.field_evidence)
        value["conflicting_fields"] = sorted(self.conflicting_fields)
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def unresolved_fields(self) -> tuple[str, ...]:
        evidenced = {name for name, _ in self.field_evidence}
        values = asdict(self)
        return tuple(
            sorted(
                name
                for name, value in values.items()
                if name not in {"field_evidence", "conflicting_fields"}
                and (value is None or name not in evidenced or name in self.conflicting_fields)
            )
        )

    @property
    def status(self) -> str:
        # Completeness is not authority verification or a discriminating test.
        return "UNCERTIFIED"

    def bind(self, *, family: str, benchmark_id: str) -> str:
        if family != self.family or benchmark_id != self.benchmark_id:
            raise ValueError("FORECAST_RULE_IDENTITY_MISMATCH")
        return self.version_id
