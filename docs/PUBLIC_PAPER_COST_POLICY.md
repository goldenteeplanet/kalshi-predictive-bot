# Public paper cost policy

The operator explicitly authorized the general one-contract taker fee model and separation of snapshot price impact from subsequent uncertainty on September 12, 2026. This policy does not certify account membership or an actual exchange invoice.

`PUBLIC_GENERAL_TAKER_CENT_PAPER_V1` computes `ceil_cent(0.07 * P * (1-P))` using Decimal for one cent-aligned-price contract. Its fee is an `ESTIMATED_WITH_SUPPORT` paper component: the current July 7 PDF formula refers to centicent rounding while examples use whole cents. The source conflict remains explicit. No account-specific support response is required for this operator-selected modeling assumption. Subcent prices need a separately reviewed policy.

Evidence replay requires the pinned public review plus original series and fee-change responses. BTC, ETH and SOL hourly event series were reviewed as quadratic multiplier 1 without a returned fee change or identified specific exception. A changed/unreviewed series or supplied account-fee override cannot silently fall back. Public review freshness is bounded; this is not an automatic override-discovery daemon.

The fee-review JSON fixture is an extraction of facts from the live web-tool PDF read, not original PDF bytes. Direct PDF downloads returned HTTP 429 twice, separated by a cooldown. Its content hash must never be described as the PDF's hash. Other fixtures contain original public API response bytes. Complete receipts, current documentation, and limitations are preserved outside the checkout under `reports/recovery/public-authority-current-20260912/` in the operator workspace.

V2 original cost records preserve their separate assessment time and re-evaluate all evidence on replay. V1 records remain unchanged. Snapshot impact is the cost to consume one contract of actual visible depth minus the selected executable ask. A missing same-side bid does not prevent buying an available opposite-side implied ask. Spread and quote movement are not charged again. Snapshot fill evidence does not establish real exchange latency or adverse selection.

The canonical validator retains unknown uncertainty and the unchanged settlement-rule certification gate. Risk and candidate assembly accept the reviewed conservative public fee after original replay, using the snapshot impact component. No returned cost assessment grants execution authority. Existing Phase 3N policy floors and all live/demo execution settings remain unchanged.

Evidence failures, unsupported calibration, no current prepared forecast, or uncertified settlement rules continue to block admission. The strict full-net-EV > $0.05 threshold is unchanged. Research numbers are historical diagnostics, never current opportunities or paper settlements.
