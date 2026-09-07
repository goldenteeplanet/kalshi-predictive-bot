# Phase 4AY — Trusted-time and clock-skew audit

Phase 4AY audits a hash-bound timestamp catalog covering every phase from 4AC through 4AX in exact
order. Each catalog entry embeds a hash-valid source artifact and explicitly declares its event and
deadline timestamp fields, avoiding heuristic timestamp discovery.

All timestamps must be timezone aware and are normalized to UTC. Event timestamps are checked
against trusted evaluation time with an explicit 0–300 second future-skew allowance. Successive
primary event times are checked for regression with a separate 0–300 second allowance. The catalog
must declare the Phase 4AX rule `VALID_IFF_NOW_STRICTLY_BEFORE_DEADLINE`, establishing that expiration
equality is expired.

Outputs are `phase4ay.time-semantics-audit.v1` and
`phase4ay.time-semantics-proof.v1`. They retain normalized timestamp evidence and deterministic
reason codes while remaining artifact-only, hash protected, and non-authorizing.

Focused tests cover complete phase coverage, UTC offset equivalence, DST fallback offsets, leap day,
future-skew and regression boundaries, naive timestamps, future dating, clock regression, source and
catalog tampering, missing phases, invalid policy limits, trusted-time mismatch, expiration semantics,
and the static read-only surface.

