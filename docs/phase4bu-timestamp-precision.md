# Phase 4BU — Timestamp Precision Harmonization

Phase 4BU canonicalizes timezone-aware wall-clock evidence to UTC with exactly six fractional
digits and verifies each duration against monotonic nanoseconds. Non-UTC offsets are accepted only
when they normalize without changing duration meaning.

Truncation, precision mismatch, timezone omission, wall or monotonic regression, sequence
regression, duration disagreement, malformed evidence, and excessive duration fail closed. The
phase is canonicalization-only and creates no records or execution authority.
