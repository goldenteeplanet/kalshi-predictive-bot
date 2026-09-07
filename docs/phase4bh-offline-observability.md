# Phase 4BH — Offline observability event schema

Phase 4BH validates eight offline event types spanning validation start/completion, refusal,
simulation start, rollback verification, artifact publication, absent/expired authorization, and
safety-invariant violation. Events contain sequence, normalized timestamp, phase, subject hash,
reason codes, and a small scalar attribute map.

Unknown fields and event types, chronology regression, invalid hashes/reasons, sensitive key names,
SQL-like text, private-key material, nested values, nulls, credentials, database paths, environment
data, production-write controls, and service commands are rejected. Outputs explicitly state that
no secrets, SQL, credentials, or production controls are present.

Outputs are `phase4bh.offline-observability-stream.v1` and
`phase4bh.observability-safety-manifest.v1`. Focused tests cover every event type and forbidden key,
SQL/key material/complex values, sequence/time/type/hash/reason failures, tampering, timezone
handling, deterministic normalization, and the artifact-only static surface.

