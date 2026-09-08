# Phase 4EW — Paper Pipeline Air-Gapped Acceptance

Phase 4EW exercises the whole synthetic paper-eligibility path with a success fixture and one
isolated fixture for every refusal class: stale market data, invalid forecast, ineligible
ranking, risk block, candidate conflict, missing operator approval, expired intent, duplicate
intent, and routing-simulation refusal. Each refusal must stop at its exact gate with its
stable reason. Success reaches the creation boundary but is structurally unable to cross it.

Acceptance requires networking, services, credentials, and order creation to be disabled and
the database mode to be `DISPOSABLE_SYNTHETIC`. Complete scenario coverage, per-fixture hashes,
outer artifact integrity, isolated gate failures, expected results, and canonical result order
are enforced. Tests also prove a disposable SQLite fixture remains byte-identical.

The implementation has no connected client or database driver. It cannot control a service,
contact an exchange, create an order, mutate production data, or authorize execution. Its only
write is atomic publication of the requested local report.
