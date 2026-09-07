# Phase 4DP — Model Confidence Calibration Latency

Phase 4DP measures supplied calibration cost in deterministic work units and identifies
safe content-addressed reuse. The reuse identity binds model, calibration version, segment,
training-data hash and cutoff, validity boundary, and calibration result hash.

Training data through the exact forecast decision instant is allowed; one microsecond of
future data is a no-lookahead violation and makes reuse ineligible. Expired artifacts are
also ineligible. Matching identities with inconsistent work cost are ambiguous and fail
closed. Wall-clock timing is not a CI gate.

The hash-protected report is atomically published and creates no calibration record. The
module has no database, network, exchange, service-control, or trading capability.
