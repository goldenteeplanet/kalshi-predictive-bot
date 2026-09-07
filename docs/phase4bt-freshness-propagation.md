# Phase 4BT — Freshness Propagation Audit

Phase 4BT validates an ordered, hash-linked freshness chain from collection through observability.
It distinguishes data age when an artifact was produced from age at audit time and applies an
inclusive per-stage freshness limit.

The audit explicitly identifies stale artifacts that claim to be current and fresh artifacts that
claim to be stale. Missing, reordered, duplicated, regressing, future, timezone-ambiguous,
tampered, or out-of-bound evidence fails closed. It creates no production records or authority.
