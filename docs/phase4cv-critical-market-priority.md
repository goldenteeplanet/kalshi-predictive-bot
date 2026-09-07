# Phase 4CV — Critical-Market Priority Model

Phase 4CV deterministically prioritizes market-data evaluation work by time remaining to an explicit evaluation timestamp. Evidence-ready, fresh markets inside the inclusive critical window rank first, followed by ready upcoming markets, blocked markets, and expired windows. Ties use ticker order.

Blocked and expired markets cannot become critical merely because their timestamps are near. The output contains only scheduling metadata: ticker, evaluation timestamp, time remaining, tier, reason, and rank. It emits no side, price, size, quantity, order type, or execution authorization.

Inputs are exact-schema, hash-protected, UTC-timestamped, uniquely identified, and bounded. Reports are deterministic, canonically hash-protected, and atomically published. The phase has no database, network, exchange, service-control, order-creation, or production-writer capability.
