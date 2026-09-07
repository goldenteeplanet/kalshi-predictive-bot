# Phase 4ET — Decision Latency SLO Report

Phase 4ET publishes deterministic latency objectives for the complete paper-eligibility
path: market data, features, forecast, ranking, risk, and operator handoff. Measurements and
objectives use integer microseconds. A stage passes at or below its objective; one microsecond
over produces its stable `STAGE_<NAME>_SLO_EXCEEDED` reason. The independently configured
end-to-end objective uses the exact sum of stage measurements and emits
`END_TO_END_SLO_EXCEEDED` when breached.

The report canonicalizes stage order, records signed headroom, binds the source artifact,
and fails closed on missing, duplicate, unknown, malformed, out-of-range, or tampered input.
This evidence cannot make a candidate eligible, authorize routing or execution, create an
order, contact a service or exchange, or read or write a database. Publication is atomic and
limited to the explicitly requested local artifact.
