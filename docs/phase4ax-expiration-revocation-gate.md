# Phase 4AX — Expiration, revocation, and supersession gate

Phase 4AX evaluates one hash-protected artifact bundle containing the proposal, review, readiness,
approval, and authorization deadlines; expected/current lineage bindings; database identity;
executor build identity; and explicit revocations. It performs no database or runtime access.

Every deadline is normalized to UTC and is valid only while evaluation time is strictly earlier.
Equality is expired. The gate reports all refusal reasons using fixed precedence: explicit
revocation, artifact supersession, database identity change, executor build supersession, then
proposal, review, readiness, approval, and authorization expiration.

Outputs are `phase4ax.unified-gate-verdict.v1` and
`phase4ax.refusal-precedence-proof.v1`. Both bind the source input and precedence, are deterministic
and hash protected, and deny execution authority. Advancement means only that this artifact-only
gate found no listed refusal; it is not permission to mutate any database or contact an exchange.

Focused tests cover each exact deadline boundary and just-before instant, every artifact and identity
supersession, revocation precedence, multiple simultaneous failures, UTC offset equivalence,
tampering, malformed bindings/deadlines/revocations, naive time, and the static read-only surface.

