# Phase 4DO — Near-Money Selection Optimization

Phase 4DO selects a bounded offline candidate set while requiring complete coverage of
both near-money candidates and candidates whose wait reaches the starvation limit. Both
boundaries are inclusive. If mandatory coverage exceeds capacity, the phase refuses
rather than silently dropping a required candidate.

Remaining capacity is filled deterministically by distance to money, longest wait, then
candidate ID. Output priority places starvation-due and near-money candidates first and
exposes every deferred ID. Invalid cycles, selection limits, distances, duplicate IDs,
malformed contracts, and hash tampering fail closed.

The deterministic report is atomically published and creates no selection or ranking
record. The module has no database, network, exchange, service-control, or trading ability.
