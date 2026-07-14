# Phase 3X — Professional Cockpit Wireframes

These are structural wireframes, not final visual specifications.

## Global shell

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ PROD | LIVE-READ-ONLY | Account | HEALTH | Freshness | As-of | 3W | 3V      │
├───────────────┬──────────────────────────────────────────────────────────────┤
│ Today         │ Page title                       Search / Command / Help     │
│ Opportunities │ Active filters and snapshot context                         │
│ Markets       ├──────────────────────────────────────────────────────────────┤
│ Portfolio     │                                                              │
│ Risk          │                    ROUTE CONTENT                              │
│ Trades        │                                                              │
│ Models        │                                                              │
│ Journal       │                                                              │
│ Research      │                                                              │
│ System        │                                                              │
│ Settings      │                                                              │
└───────────────┴──────────────────────────────────────────────────────────────┘
```

## Today

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Today · Tuesday, session context · snapshot age · refresh evidence          │
│ Ask: What should I trade today?                                              │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Portfolio & risk                 │ System / data warnings                    │
│ Net P&L                          │ Market feed aging                         │
│ Daily loss used / headroom       │ Forecast pipeline fresh                   │
│ Drawdown / limit                 │ 3W certificate valid                      │
│ Exposure / concentration         │ 3V status                                 │
├──────────────────────────────────┴───────────────────────────────────────────┤
│ Ranked opportunities                                                         │
│ #  Market  Side  Px  Mkt%  Model%  Edge  RA-EV  Conf  Liq  3S  3M  3N      │
│ 1  ...                                                                      │
│ 2  ...                                                                      │
│ [Blocked and excluded: N] [Why this ranking?]                                │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ What changed                     │ Nightly journal                            │
│ market/model/risk changes        │ Worked · Failed · Changed                 │
└──────────────────────────────────┴───────────────────────────────────────────┘
```

## Opportunity detail

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Market title · YES/NO side · status · valid until · freshness               │
│ Thesis · key reasons · invalidation conditions                               │
├──────────────────────┬──────────────────────┬────────────────────────────────┤
│ Market probability   │ Model probability    │ Risk-adjusted EV               │
│ price / spread       │ confidence / support │ gross / costs / adjusted       │
├──────────────────────┴──────────────────────┴────────────────────────────────┤
│ 3S PROCEED/SKIP → 3M proposed quantity → 3N ALLOW/REDUCE/BLOCK              │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Portfolio impact                 │ Liquidity and execution                    │
├──────────────────────────────────┴───────────────────────────────────────────┤
│ Tabs: Forecast | Economics | Risk | Evidence | History                       │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Portfolio and risk

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ P&L | Daily loss | Drawdown | Worst-case loss | Risk budget                  │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Exposure by selected dimension   │ Limits and headroom                       │
│ exact chart + table              │ portfolio / market / trade                │
├──────────────────────────────────┴───────────────────────────────────────────┤
│ Contributors: positions | open orders | reservations | projected             │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Risk decision

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Decision: BLOCK · reason summary · as-of                                    │
├──────────────────────────────────────────────────────────────────────────────┤
│ Opportunity → 3S → 3M → Portfolio checks → Market checks → Trade checks     │
│                              PASS       FAIL            NOT RUN              │
├──────────────────────────────────┬───────────────────────────────────────────┤
│ Limit used / projected / max     │ Contributing exposure                     │
├──────────────────────────────────┴───────────────────────────────────────────┤
│ Stable reason codes · evidence · lineage                                    │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Mobile monitoring

```text
┌──────────────────────────────┐
│ PROD · LIVE · STALE · 3W     │
├──────────────────────────────┤
│ Today                        │
│ Risk snapshot                │
├──────────────────────────────┤
│ Top opportunity card         │
│ side / price                 │
│ model vs market              │
│ edge / RA-EV                 │
│ 3S / 3M / 3N                 │
│ valid until                  │
├──────────────────────────────┤
│ Warnings                     │
├──────────────────────────────┤
│ Bottom navigation            │
└──────────────────────────────┘
```
