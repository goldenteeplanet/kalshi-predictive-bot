# Market data environment validation

The cloud stream combined a demo WebSocket with production REST market metadata. Repeated stored weather snapshots contained empty demo book arrays alongside changing production quote fields. A healthy stream status did not establish usable production liquidity.

The adapter now validates exact documented endpoint schemes, hosts and API paths and requires both sources to use the same environment before connection. It rechecks before staging, records both endpoints and their environment, and builds the market source URL from the actual REST base. The drain checks staged provenance against its configured REST environment before insertion. Unknown legacy provenance and mismatches remain on disk and are reported; no source labels are invented for old snapshots.

The patch does not change credentials, configure production authentication, renew fee policies, relax qualification gates, or restart services. A bounded public production REST book route remains necessary until production stream authentication is demonstrated. Deployment must include that operational path and preserve original staging archives. Existing demo stream records must not be treated as production evidence.

Validation: 30 focused tests covering stream reconstruction/watch behavior and new endpoint/drain rejection cases; Ruff passed on four changed Python files. Local test environment explicitly loaded this worktree's src. No cloud validation or deployment yet.

Endpoint reference: https://docs.kalshi.com/getting_started/api_environments
