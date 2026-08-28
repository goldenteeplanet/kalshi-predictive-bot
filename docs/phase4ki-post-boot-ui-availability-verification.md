# Phase 4KI: Post-boot UI availability verification

Phase 4KI adds the final availability probe to the post-boot chain. It requires Phase 4KH writer exclusivity and evaluates bounded, content-addressed evidence showing an HTTP 200 response from the expected UI identity within the freshness and duration limits.

Unavailable or timed-out responses fail; stale, incomplete, or integrity-unverified evidence blocks; response-identity mismatch is treated as tampering. The decision grants no recovery, restart, service-control, or execution authority. The verifier performs no network, browser, service, or host operation itself.
