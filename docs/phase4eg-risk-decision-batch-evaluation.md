# Phase 4EG — Risk Decision Batch Evaluation

Phase 4EG evaluates multiple synthetic candidates independently against one immutable,
freshness-gated portfolio snapshot. Each candidate references the same snapshot hash and
uses exact Decimal comparisons for candidate caps, available capital, and portfolio
exposure. Boundary equality passes; any excess refuses with stable reason ordering.

Evaluation never carries state from one candidate to the next. Available capital and
current exposure before and after every candidate are identical, and input order cannot
change results. Mutable or mixed snapshots, invalid hashes or Decimals, duplicate candidate
identities, schema drift, and artifact tampering fail closed.

This model reserves no capital, mutates no snapshot, creates no decision or order, and
publishes atomically without database or exchange access.
