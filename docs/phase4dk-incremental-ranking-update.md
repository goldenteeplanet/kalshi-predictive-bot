# Phase 4DK — Incremental Ranking Update

Phase 4DK applies a bounded set of offline candidate upserts and deletions to a verified
prior candidate state. Before updating, the supplied previous ranking must equal a fresh
full ranking under exact Decimal score descending and `candidate_id` ascending semantics.

Changed candidates are identified explicitly. The resulting incremental ranking is
compared item-for-item and by canonical hash with an independently rebuilt full ranking.
Stale prior ordering, duplicate updates, invalid scores, missing deletes, empty results,
malformed inputs, and hash tampering fail closed.

The deterministic report is published atomically and creates no ranking record. The
module has no database, network, exchange, service-control, or trading capability.
