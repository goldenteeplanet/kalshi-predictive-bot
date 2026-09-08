# Phase 4KD — Post-boot WSL verification

Phase 4KD verifies that a guarded restart actually crossed a Windows boot boundary and that WSL plus the hashed authoritative distro are available afterward. Evidence must be captured after the bound restart intent and pass probe-integrity checks.

An unchanged boot identity or pre-intent observation is tampered. Missing, partial, or unverified evidence is incomplete. Unavailable WSL or a non-running authoritative distro fails and stops the post-boot chain.

`PASS` permits only the next read-only verification stage. It grants no recovery, restart, service-control, or execution authority. The verifier has no WSL command, filesystem, process, database, notification, or host-control surface.
