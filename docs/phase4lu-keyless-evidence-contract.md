# Phase 4LU — Canonical Evidence Signing Request and Keyless Proof Contract

## Outcome

Phase 4LU creates inert signing-request metadata for canonical Phase 4LR–4LT evidence and validates
supplied keyless proof metadata offline. Requests bind evidence, purpose, logical issuer, audience,
time window, nonce digest, algorithm, and transparency intent without retaining the raw nonce.

## Verification boundary

The validator checks request integrity, identity and audience bindings, bounded clocks, replay sets,
signature metadata binding, and Merkle inclusion structure. It intentionally does not accept public
keys or signature bytes and therefore reports cryptographic signature verification as false. A later
cryptographic verifier may consume this contract without changing its authority boundary.

## Reproducible contract evidence

The fixed offline fixture returns `PASS` with request SHA-256
`dc249d630c07625e492fbea9f6dc893bde8e69790e3bbe9ebb3ded172a6a2768`, proof SHA-256
`8b9fcf4ac2757de76748a3dae3240707c9e769bfc355f6fa2142c7df101346de`, and validation
SHA-256 `82aaa4a17279ecec1455cf95132e1e099493c81070c5990ed95ce7057bfb5882`.

## Safety and removal

No private key, credential, bearer token, endpoint, signing operation, trust lookup, or network call
is present. The code cannot import or generate keys, control services, or create orders. Remove the
script, focused test, and report to roll back.

## Next phase

Phase 4LV — Offline cryptographic verifier interface and trust-root pinning specification.
