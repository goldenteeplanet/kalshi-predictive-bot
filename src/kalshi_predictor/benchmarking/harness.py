from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from kalshi_predictor.benchmarking.agents import BenchmarkAgent, OrderIntent
from kalshi_predictor.benchmarking.replay import SyntheticEpisode, replay_digest, replay_episode
from kalshi_predictor.kalshi.orderbook import LocalOrderbook


@dataclass(frozen=True)
class TradeLogRow:
    timestamp: str
    ticker: str
    outcome: str
    action: str
    requested_size: str
    filled_size: str
    average_price: str
    gross_value: str
    fee: str
    slippage: str
    reason: str


@dataclass(frozen=True)
class BenchmarkResult:
    episode_id: str
    category: str
    agent_name: str
    replay_digest: str
    initial_cash: Decimal
    final_cash: Decimal
    final_equity: Decimal
    trades: tuple[TradeLogRow, ...]
    equity_curve: tuple[dict[str, str], ...]
    metrics: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id, "category": self.category,
            "agent_name": self.agent_name, "replay_digest": self.replay_digest,
            "initial_cash": str(self.initial_cash), "final_cash": str(self.final_cash),
            "final_equity": str(self.final_equity),
            "trades": [asdict(row) for row in self.trades],
            "equity_curve": list(self.equity_curve), "metrics": self.metrics,
        }


def run_benchmark(
    episode: SyntheticEpisode,
    agent: BenchmarkAgent,
    *,
    initial_cash: Decimal = Decimal("100"),
    taker_fee_rate: Decimal = Decimal("0.07"),
) -> BenchmarkResult:
    frames = replay_episode(episode)
    agent.reset()
    cash = initial_cash
    positions: dict[tuple[str, str], Decimal] = {}
    trades: list[TradeLogRow] = []
    curve: list[dict[str, str]] = []
    for frame in frames:
        book = LocalOrderbook(frame.ticker)
        book.apply_rest_snapshot(frame.orderbook, resume_sequence=frame.sequence or 0)
        for intent in agent.act(frame):
            cash, trade = _execute(intent, frame.timestamp.isoformat(), book, cash,
                                   positions, taker_fee_rate)
            if trade is not None:
                trades.append(trade)
        equity = _marked_equity(cash, positions, {frame.ticker: book})
        curve.append({"timestamp": frame.timestamp.isoformat(), "equity": str(equity)})
    for (ticker, outcome), size in positions.items():
        if episode.settlements.get(ticker) == outcome:
            cash += size
    final_equity = cash
    if curve:
        curve.append({"timestamp": "settlement", "equity": str(final_equity)})
    metrics = _metrics(initial_cash, final_equity, curve, trades)
    return BenchmarkResult(
        episode.episode_id, episode.category, agent.name, replay_digest(frames),
        initial_cash, cash, final_equity, tuple(trades), tuple(curve), metrics,
    )


def _execute(intent: OrderIntent, timestamp: str, book: LocalOrderbook, cash: Decimal,
             positions: dict[tuple[str, str], Decimal], fee_rate: Decimal
             ) -> tuple[Decimal, TradeLogRow | None]:
    quote = book.execution_quote(
        outcome=intent.outcome, action=intent.action, size=intent.size,
    )
    if quote.filled_size <= 0 or quote.average_price is None or intent.action != "buy":
        return cash, None
    fee = _fee(quote.filled_size, quote.average_price, fee_rate)
    cost = quote.total_value + fee
    if cost > cash:
        return cash, None
    best = book.best_yes_ask if intent.outcome == "yes" else book.best_no_ask
    slippage = quote.average_price - best if best is not None else Decimal("0")
    positions[(intent.ticker, intent.outcome)] = (
        positions.get((intent.ticker, intent.outcome), Decimal("0")) + quote.filled_size
    )
    return cash - cost, TradeLogRow(
        timestamp, intent.ticker, intent.outcome, intent.action,
        str(intent.size), str(quote.filled_size), str(quote.average_price),
        str(quote.total_value), str(fee), str(slippage), intent.reason,
    )


def _fee(size: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    cents = rate * size * price * (Decimal("1") - price) * Decimal("100")
    return cents.to_integral_value(rounding=ROUND_CEILING) / Decimal("100")


def _marked_equity(cash: Decimal, positions: dict[tuple[str, str], Decimal],
                   books: dict[str, LocalOrderbook]) -> Decimal:
    value = cash
    for (ticker, outcome), size in positions.items():
        book = books.get(ticker)
        if book is None:
            continue
        price = book.midpoint
        if price is not None:
            value += size * (price if outcome == "yes" else Decimal("1") - price)
    return value


def _metrics(initial: Decimal, final: Decimal, curve: list[dict[str, str]],
             trades: list[TradeLogRow]) -> dict[str, Any]:
    peak = initial
    max_drawdown = Decimal("0")
    for row in curve:
        equity = Decimal(row["equity"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "total_pnl": str(final - initial), "return": str((final - initial) / initial),
        "max_drawdown": str(max_drawdown), "trade_count": len(trades),
        "fees_paid": str(sum((Decimal(row.fee) for row in trades), Decimal("0"))),
    }
