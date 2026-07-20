from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from kalshi_predictor.benchmarking.replay import ReplayFrame
from kalshi_predictor.utils.decimals import to_decimal


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    outcome: str
    action: str
    size: Decimal
    reason: str


class BenchmarkAgent(Protocol):
    name: str

    def reset(self) -> None: ...
    def act(self, frame: ReplayFrame) -> list[OrderIntent]: ...


@dataclass
class PassiveAgent:
    name: str = "passive"

    def reset(self) -> None:
        return None

    def act(self, frame: ReplayFrame) -> list[OrderIntent]:
        return []


@dataclass
class SeededRandomAgent:
    seed: int = 0
    trade_probability: float = 0.25
    size: Decimal = Decimal("1")
    name: str = "seeded_random"
    _random: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._random = random.Random(self.seed)

    def act(self, frame: ReplayFrame) -> list[OrderIntent]:
        if self._random.random() >= self.trade_probability:
            return []
        outcome = "yes" if self._random.random() < 0.5 else "no"
        return [OrderIntent(frame.ticker, outcome, "buy", self.size, "seeded_random")]


@dataclass
class MomentumAgent:
    threshold: Decimal = Decimal("0.01")
    size: Decimal = Decimal("1")
    name: str = "momentum"
    _previous: dict[str, Decimal] = field(default_factory=dict, init=False, repr=False)

    def reset(self) -> None:
        self._previous.clear()

    def act(self, frame: ReplayFrame) -> list[OrderIntent]:
        midpoint = to_decimal(frame.orderbook["gh1_local_orderbook"].get("midpoint"))
        previous = self._previous.get(frame.ticker)
        if midpoint is None:
            return []
        self._previous[frame.ticker] = midpoint
        if previous is None or abs(midpoint - previous) < self.threshold:
            return []
        outcome = "yes" if midpoint > previous else "no"
        return [OrderIntent(frame.ticker, outcome, "buy", self.size, "midpoint_momentum")]
