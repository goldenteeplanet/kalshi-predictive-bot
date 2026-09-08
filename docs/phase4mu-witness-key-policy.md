# Phase 4MU — Witness-Key Lifecycle and Policy Recovery

## Outcome

Phase 4MU binds attestations to explicit witness keys and independently pinned threshold-policy
versions. It models issuance, activation, overlap rotation, expiry, revocation, compromise time,
historical verification, threshold upgrades, evidenced emergency downgrades, and rollback refusal.

## Recovery boundary

Compromise recovery requires the affected key to be contained, every affected independence group to
receive a replacement key, and the original threshold to remain intact. Attestations created before
the recorded compromise remain historically verifiable; attestations at or after compromise refuse.
Recovery certification grants neither acceptance nor repair authority.

## Reproducible evidence

- complete Phase 4MP–4MU suite: 61 passed
- head-policy SHA-256: `b45832080f9d50028a133f021134224e69a8101be308a95c004b128de7e047d8`
- policy-chain SHA-256: `89f593778a2f8e553a6469f9243bba13be1eaa22ccd5fd67f6463cd0b97bce33`
- key-bound verification SHA-256: `d9a768a5ef796bac3a6dcc0947baf49f88a318eacb749e4f72e0556124883295`
- compromise-recovery SHA-256: `79e7f27a4320690431907d6c74e33e4f7736a314dbc8e173737bba993da8e446`

## Safety and removal

This is deterministic offline evidence. It has no persistence, network, runtime, service, repair,
acceptance, or order capability. Remove the script, focused test, and report to roll back.

## Next phase

Phase 4MV — Threshold-signature transcript simulation and signer-subset audit proof.
