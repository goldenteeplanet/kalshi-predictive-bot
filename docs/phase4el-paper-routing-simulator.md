# Phase 4EL — Paper Routing Simulator

Phase 4EL models paper routing in memory with deterministic duplicate, quantity, price, and
fill stages. Duplicate groups select the lexicographically smallest intent identity as the
canonical primary, making results independent of input order. Quantity and integer-cent
price limits stop at their exact stage; valid intents produce deterministic full, partial,
or unfilled synthetic outcomes.

Each stage has supplied deterministic latency units, so refusal and success paths expose
auditable latency without wall-clock dependence. Boundary prices pass, while malformed
policies, hashes, quantities, stage latencies, duplicate identities, schema drift, and
artifact tampering fail closed.

The simulator creates zero real paper orders or fills, performs zero database writes, has
no connected routing capability, and publishes only an atomic artifact.
