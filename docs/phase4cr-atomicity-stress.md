# Phase 4CR — Snapshot Publication Atomicity Stress Test

Phase 4CR exercises actual filesystem replacement and concurrent readers only inside internally created disposable directories. It writes a valid initial artifact, creates a deliberately truncated temporary artifact, verifies that the published target remains valid, and then runs simultaneous readers immediately before and after every atomic replacement.

Every observed artifact must have valid JSON, a matching canonical hash, and a generation marker consistent with its generation. Readers must see the complete prior generation before replacement and the complete new generation afterward. Recovery deletes stale same-target temporary files and revalidates the final artifact. Exact generation and reader bounds prevent unbounded stress.

The stress report is deterministic, hash-protected, and optionally atomically published to a caller-specified report path. Snapshot stress itself touches only a `TemporaryDirectory`; it has no database, network, exchange, service-control, or production-writer capability.
