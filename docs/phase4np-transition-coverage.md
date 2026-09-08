# Phase 4NP — Coverage-Guided Transition Saturation

## Outcome

Phase 4NP measures states, commands, transitions, adjacent transition pairs, refusal classes,
terminal states, checkpoint/restart behavior, and unsafe accepted states. It retains sequences only
when they add coverage, deterministically mutates the retained corpus, stops after bounded coverage
saturation, and prunes sequences that are redundant for the observed token set.

The proof requires every declared state, command, critical transition, and refusal class. It refuses
unreachable requirements, coverage regression, invalid accounting, corpus-bound violations, unsafe
accepted states, missing restart coverage, hash drift, and failure to reach saturation.

## Verification evidence

- Focused Phase 4NO–4NP suite: `15 passed`
- Saturation rounds: `3`
- Minimized corpus sequences: `9`
- Coverage: `8` states, `12` commands, `17` transitions, `16` transition pairs, `5` refusals
- Coverage SHA-256: `6a51364c8b44a4872d0bdb1b9b7c367ed3c94e2c0efcc6039e28c4a69927933e`
- Saturation verdict: `PASS`
- Proof SHA-256: `63c1fc757540f0ba4ec5f5f953144f6ad021aeff3071e399c6b628db9d1ebf2b`

## Safety and removal

Exploration is deterministic, offline, and non-persistent, with no paper, demo, live, autopilot,
order, network, or runtime mutation capability. Remove the three phase files to roll back.

## Next phase

Phase 4NQ — Model-based oracle comparison and transition semantics proof.
