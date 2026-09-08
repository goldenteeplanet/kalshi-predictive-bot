# Phase 4IM — Clock-source evidence capture

Phase 4IM canonicalizes captured NTP, Windows-host, and signed-time measurements. Source identities are hashed; addresses, server names, and raw output are excluded. Evidence preserves offset, uncertainty, synchronization state, timestamp, and NTP stratum where applicable.

Defaults limit a capture to eight sources and absolute offsets to 24 hours; exact bounds pass. Empty or incomplete evidence is `PARTIAL`; excessive offsets are refused; duplicate identities, future data, invalid source/stratum combinations, malformed data, or tampering fails closed.

The normalizer never queries or changes NTP, Windows time, or the system clock. It grants no recovery, service-control, host restart, order, or execution authority.
