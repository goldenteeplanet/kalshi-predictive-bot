# Phase 4CA — Market Catalog Delta Planner

Phase 4CA compares hash-protected previous and discovered catalog summaries and produces an
artifact-only refresh plan. Unchanged markets require both the same revision and content hash;
changed and new markets are fetched, removed markets are recorded explicitly, and outputs are
sorted deterministically.

Incremental operation cannot displace full reconciliation. An initial catalog, explicit force flag,
or 24 completed incremental cycles produces a full checkpoint that fetches every discovered market.
Malformed or duplicate identities, invalid revisions or hashes, and tampered inputs fail closed.
The planner does not call an API, mutate a database, alter a collector, or authorize execution.
