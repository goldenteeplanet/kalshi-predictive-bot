# Phase 4MC — Minimal-Repair Plan Mutation Audit and Non-Escalation Proof

## Outcome

Phase 4MC independently validates Phase 4MB plan structure, hashes, actions, safety fields, and
minimality fields, then compares a candidate with its trusted baseline. Even a correctly rehashed
mutation is refused when it widens trust, changes the corruption boundary, suppresses evidence or
revalidation work, invents hashes, authorizes edits, or adds operational capability.

## Mutation certification

The deterministic corpus covers top-level bindings, trusted-prefix and boundary fields, every
mandatory repair action, every minimality prohibition, and every safety capability. Certification
passes only when the baseline is valid and every mutation is refused.

The reference run refused all 28 of 28 mutations and produced audit SHA-256
`9f7f8c54335951bfe5534ea24a70d94cd8bd9e7e082ab38c0aa9ed1ed0d8b2d8`. An unchanged candidate
produced identity-audit SHA-256
`6c5b336b43f37d8ff7cff078baa7f0d801f6203c871286a5fdbbc1b15c6406da`.

## Safety and removal

The audit is read-only and offline. It cannot edit or repair checkpoints, write runtime state,
control WSL or services, access the network, or create any live, demo, autopilot, or paper order.
Remove the script, focused test, and this report to roll back.

## Next phase

Phase 4MD — Repair-plan authorization envelope and single-use execution-token model.
