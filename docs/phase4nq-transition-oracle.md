# Phase 4NQ — Model-Based Transition Oracle

## Outcome

Phase 4NQ specifies state-machine semantics as declarative rules interpreted independently from the
Phase 4NO implementation. Rules define commands, source-state guards, target states, checkpoint
restoration, terminal behavior, and refusal effects. The oracle compares normalized traces,
acceptance decisions, refusal sets, terminal states, provenance, and implementation hash chains.

The proof runs the minimized Phase 4NP corpus plus newly generated sequences. Rule audits reject
missing commands, duplicate identities, overlapping guards, unknown states or targets, and other
unreachable rules. Differential checks reject semantic, terminal, refusal, provenance, checkpoint,
hash-chain, and safety disagreement.

## Verification evidence

- Focused Phase 4NP–4NQ suite: `14 passed`
- Declarative oracle rules: `13`
- Compared sequences: `57`
- Implementation/oracle disagreements: `0`
- Rules SHA-256: `59ddc739f1d34f9f3fc027fad2c3dde32f4a48e823f1c334689f4f5651cfa2ac`
- Oracle verdict: `PASS`
- Proof SHA-256: `34d175369e861d88a7de345e2f0fd4964649357f721118b4b66f20e211a3fb7f`

## Safety and removal

The oracle is deterministic, offline, and non-persistent, with no paper, demo, live, autopilot,
order, network, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NR — Long-horizon sequence soak and deterministic checkpoint-resume proof.
