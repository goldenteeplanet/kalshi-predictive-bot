# Phase 4NR — Long-Horizon Checkpoint/Resume Soak

## Outcome

Phase 4NR generates deterministic rotated multi-epoch workloads containing thousands of commands,
repeated verification, refusal recovery, interruption/restart flows, and archive handoffs. Each
epoch has stable source provenance and identity, and normalized summaries form a cumulative hash
chain.

The runner signs periodic checkpoints and compares uninterrupted execution with resumption from
every checkpoint. Suffix traces, terminal states, refusal classes, provenance, coverage, and final
hashes must agree. It also enforces command floors, epoch order and uniqueness, runtime and encoded
memory bounds, state-machine safety, and deterministic attested output.

## Verification evidence

- Focused Phase 4NR suite: `7 passed`
- Workload: `1,970` commands across `160` epochs
- Signed checkpoints/resume comparisons: `4`
- Encoded normalized trace: `88,574` bytes
- Observed soak runtime: `9.479` seconds
- Workload SHA-256: `b64671072eccd5217098d3ecf55f6676db1166f864cff386cab718cf47abcff8`
- Final cumulative SHA-256: `c70a80992e61969edd34fa6391a752d8f65319aafc870e2210bb808cdbb5b09c`
- Soak verdict: `PASS`
- Proof SHA-256: `7161610460fbdee6c28c0c3c2257c311e558bcb0c684b3651e43aa3472f5ce7f`

## Safety and removal

The soak is offline and non-persistent, with no paper, demo, live, autopilot, order, network, service,
or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NS — Fault-injected checkpoint durability and recovery matrix.
