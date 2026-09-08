# Phase 4BF — Artifact and parser fuzzing

Phase 4BF applies eleven deterministic mutation classes to a complete ordered target catalog covering
Phase 4AC through 4BE: truncated JSON, excessive depth, extreme integers, duplicate logical keys,
Unicode edges, invalid paths, malformed timestamps, oversized lists, hash confusion, unexpected
nulls, and type substitution.

The shared parser checks byte size before decoding, detects duplicate keys with an object-pairs hook,
enforces depth/list/integer limits, validates strict fields, safe relative paths, timezone-aware
timestamps, exact schema, and canonical hash. Unicode combining and non-ASCII values are accepted
only when canonical hashing succeeds; every unsafe mutation fails closed.

Outputs are `phase4bf.deterministic-fuzz-report.v1` and
`phase4bf.parser-bounds-proof.v1`. Focused tests cover every mutation independently, full target
coverage/order, duplicate targets, invalid hash fields, catalog tampering, byte-limit precedence,
timezone handling, and the absence of external/database execution capability.

