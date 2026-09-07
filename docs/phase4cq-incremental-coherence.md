# Phase 4CQ — Incremental Coherence Validator

Phase 4CQ models validation of only changed snapshot components while preserving periodic full validation and a deterministic equivalence proof. Unchanged component results may be carried only from a hash-valid full attestation that exactly matches the previous snapshot and every previous component hash.

Before the configured boundary, changed component hashes define the incremental validation scope. At `cycles_since_full + 1 == full_validation_interval`, every component is validated. Global timestamp coherence is always checked. An offline full-reference evaluation independently validates every current component; the report is emitted only when its component statuses and overall verdict exactly match the incremental result. The proof inputs receive a canonical hash.

This phase changes no runtime validator. Inputs and outputs are bounded, exact-schema, hash-protected, deterministic, and atomically published. It has no database, network, exchange, service-control, or production-writer capability.
