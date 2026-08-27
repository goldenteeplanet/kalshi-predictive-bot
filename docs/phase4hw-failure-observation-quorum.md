# Phase 4HW — Failure observation quorum

Phase 4HW requires agreement from at least two distinct, complete, fresh observers before an allowlisted dependency failure is considered proven. Evidence is bound to the Phase 4HV dependency decision, canonicalized, bounded, and integrity-hashed.

Repeated observations from one source do not increase quorum. Mixed dependencies, duplicate observation identities, future evidence, incomplete evidence, stale evidence, or a dependency that was not allowlisted fail closed. The default freshness window is 120 seconds and its endpoint is inclusive.

A `QUORUM` result is evidence only. It cannot perform recovery, control a service, restart WSL or Windows, create an order, or authorize execution.
