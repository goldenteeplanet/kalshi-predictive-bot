# Phase 4EJ — Paper Eligibility Handoff Contract

Phase 4EJ creates a compact, hash-protected handoff that keeps forecast quality, ranking
eligibility, risk eligibility, operator approval, and routing eligibility visibly separate.
Each upstream stage contributes only its decision-critical status, stable reasons, and full
artifact hash; risk also contributes the exact allowed quantity.

Routing eligibility is the conjunction of all four upstream gates. Failed stages remain
independently visible, malformed quantities or approvals fail closed, and reason lists are
canonicalized. Passing routing eligibility is not order authority: paper-order creation and
execution remain unconditionally false and the artifact creates zero orders.

The handoff performs no database, service, exchange, or routing action and publishes
atomically for downstream offline inspection.
