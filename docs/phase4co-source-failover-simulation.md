# Phase 4CO — Source Failover Simulation

Phase 4CO simulates source outages entirely from supplied artifacts. The primary source remains selected while available. An alternate may be selected only when a pairwise equivalence claim exists, the claim references the current contract hashes of both sources, and schema, units, timestamp meaning, and tolerance are all explicitly proven equivalent.

Missing, incomplete, or stale equivalence evidence causes `REFUSE`; it is never inferred from similar names or values. Candidate selection is deterministic by unique priority. The scenario report distinguishes primary use, proven failover, rejected candidates, and refusal.

Inputs and reports are canonically hash-protected, schemas and identifiers are exact, collections are bounded, and publication is atomic. This is simulation only: it makes zero network calls and never applies runtime failover or accesses a database, exchange, service manager, or production writer.
