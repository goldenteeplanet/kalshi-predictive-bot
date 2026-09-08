# Phase 4DR — Settlement Evaluation Incrementality

Phase 4DR applies supplied settlement upserts and deletions to a verified baseline and
recomputes only affected market metrics. A move between markets affects both the old and
new market; a deleted final settlement removes that market's metrics.

Metrics use exact Decimal Brier sums, settlement counts, and deterministic correctness at
the 0.5 boundary. The previous metrics must equal a fresh pre-update rebuild, and the final
incremental map must equal an independent full post-update rebuild by value and hash.

Invalid probabilities or outcomes, duplicate updates, missing deletes, empty evaluation,
stale metrics, malformed evidence, and tampering fail closed. Atomic publication creates
no evaluation record and has no database, network, exchange, service, or trading ability.
