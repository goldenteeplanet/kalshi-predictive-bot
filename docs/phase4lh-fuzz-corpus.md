# Phase 4LH — Evidence Parser Fuzz Corpus and Deterministic Minimizer

## Outcome

Phase 4LH adds a bounded seeded fuzz corpus spanning valid inputs, duplicate keys, truncation,
encoding corruption, numeric abuse, depth, fan-out, Unicode controls, path confusion, schema
mutation, capability-bearing extensions, trailing data, and oversized strings. Stable case IDs and
hashes make every run reproducible.

The deterministic minimizer removes byte ranges while preserving the exact Phase 4LG failure
signature and records source/minimized identities plus a strict evaluation budget.

## Safety and removal

Generation and minimization operate only on bounded in-memory byte strings. There is no database,
filesystem publication, network, service, writer-lock, decompression, or trading capability. Remove
the script, focused test, and report to roll back.

## Next phase

Phase 4LI — Evidence corpus mutation-coverage and blind-spot audit.
