# Phase 4EQ — Operator Decision Packet Compression

Phase 4EQ builds a compact, artifact-only review packet. It retains exactly ten
decision-critical fields: market, side, quantity, price, expected edge, expiration,
eligibility, reason codes, binding cap, and whether operator review is required.

The packet also carries a canonical, role-sorted provenance manifest. Every full evidence
artifact remains reachable through its locator and SHA-256 hash, while the manifest hash
binds the entire collection. Duplicate roles, missing evidence, malformed digests, schema
drift, inconsistent eligibility and quantity, manifest mismatch, or input tampering fail
closed. Artifact order cannot change the normalized decision or provenance content.

This phase does not record approval, authorize routing, create paper orders, contact an
exchange, or read or write a database. Publication is an atomic local artifact replacement.
