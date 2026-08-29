# Phase 4MK — Independent Reproducibility and Clean-Checkout Audit

## Outcome

Phase 4MK creates a no-hardlinks local clone in a verified temporary boundary, checks out an exact
descendant commit, independently verifies all 75 Phase 4LK–4MI blobs, regenerates Phase 4MJ
certification, compares normalized certification semantics, and runs a representative test partition.

## Isolation and refusal

Normalization removes only checkout path, current descendant HEAD, and the resulting outer hash;
all certified range semantics remain exact. The audit refuses wrong range or checkout heads, missing
or corrupt blobs, schema drift, untracked phase substitution, stale test evidence, test failure,
nondeterministic semantics, certification mismatch, and unverified temporary cleanup.

## Reproducible evidence

- Verified isolated blobs: 75 of 75.
- Representative clean-clone tests: 57 passed.
- Inventory SHA-256: `a66d0fd84d4b7f4d5a39a5400f26100a91b9c8ff466e19dc8fbffd54d2c86171`
- Normalized certification semantics SHA-256:
  `b41600a779dbcb61b2f37df6898319f91670f1642d0d18017adb225945ba38c3`
- Certification comparison SHA-256:
  `1abe142b4f5f4a3f36f4ddac776398817d09201bd277378a8b20907cfe91330b`
- Reproducibility audit SHA-256:
  `a19f214178cbf2fe45b95686cca1e0cbfe1fbb3cd9aad8ca899491e996b6644a`

Two independent default runs produced the same audit hash and both verified temporary cleanup.

## Safety and removal

The clone is local and temporary, and cleanup is verified after the context closes. The primary
checkout is never changed. The audit does not push, access trading APIs, change runtime state,
control WSL or services, access the network, or create any order. Remove the script, focused test,
and this report to roll back.

## Next phase

Phase 4ML — Recovery certification artifact packaging and offline handoff contract.
