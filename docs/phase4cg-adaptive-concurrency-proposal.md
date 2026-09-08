# Phase 4CG — Adaptive Concurrency Proposal

Phase 4CG converts captured latency, throttle, timeout, and sample-count windows into a bounded,
non-executing concurrency proposal. Degradation at the exact throttle or timeout threshold proposes
an immediate halving; insufficient evidence holds; consistently low error and below-target latency
can propose only a one-step increase, never above 32.

Malformed or out-of-order windows, impossible rates, invalid bounds, and tampering fail closed. The
artifact does not change live settings, execute fetches, call providers, mutate databases, control
services, or authorize trading. Any future integration remains a separate, explicitly approved and
shadow-validated action.
