from __future__ import annotations

from dataclasses import dataclass, field

from app.exchanges.base import Candle
from app.paper_trading.simulator import PaperTradingSimulator
from app.strategies.base_strategy import BaseStrategy, StrategyContext


@dataclass(slots=True)
class BacktestReport:
    total_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    average_win: float
    average_loss: float
    expectancy: float
    best_setup: str
    worst_setup: str
    session_performance: dict[str, float]
    coin_performance: dict[str, float]
    equity_curve: list[float] = field(default_factory=list)


class Backtester:
    def __init__(self, starting_equity: float = 10_000.0) -> None:
        self.starting_equity = starting_equity

    def run(
        self,
        symbol: str,
        candles: list[Candle],
        strategy: BaseStrategy,
        session_name: str = "london",
    ) -> BacktestReport:
        simulator = PaperTradingSimulator(starting_equity=self.starting_equity)
        setup_pnl: dict[str, float] = {}
        for index in range(60, len(candles)):
            rolling = candles[: index + 1]
            context = StrategyContext(
                symbol=symbol,
                candles_by_timeframe={"5m": rolling, "3m": rolling, "1m": rolling, "15m": rolling},
                session_name=session_name,
                regime="good",
                coin_strength_score=75,
                btc_direction="long",
                eth_direction="long",
                asian_high=max(candle.high for candle in rolling[-40:-10]),
                asian_low=min(candle.low for candle in rolling[-40:-10]),
                intraday_high=max(candle.high for candle in rolling[-30:]),
                intraday_low=min(candle.low for candle in rolling[-30:]),
            )
            signal = strategy.evaluate(context)
            if signal.accepted and not simulator.positions:
                quantity = max(0.001, (simulator.equity * 0.01) / max(signal.entry_price, 1e-12))
                simulator.open_position(
                    symbol,
                    signal.direction,
                    quantity,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit_levels,
                )
                setup_pnl.setdefault(signal.setup_name, 0.0)
            fills = simulator.update_price(symbol, candles[index].close)
            for fill in fills:
                setup_pnl[strategy.name] = setup_pnl.get(strategy.name, 0.0) + fill.pnl

        for position in list(simulator.positions.values()):
            simulator.close_position(position.id, candles[-1].close, "backtest_end")

        pnls = [fill.pnl for fill in simulator.fills]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        total = len(pnls)
        best_setup = max(setup_pnl, key=setup_pnl.get) if setup_pnl else strategy.name
        worst_setup = min(setup_pnl, key=setup_pnl.get) if setup_pnl else strategy.name
        average_win = gross_win / len(wins) if wins else 0.0
        average_loss = sum(losses) / len(losses) if losses else 0.0
        win_rate = len(wins) / total * 100 if total else 0.0
        expectancy = (sum(pnls) / total) if total else 0.0
        return BacktestReport(
            total_trades=total,
            win_rate=round(win_rate, 2),
            profit_factor=round(gross_win / gross_loss, 2) if gross_loss else float("inf") if gross_win else 0.0,
            max_drawdown=round(simulator.max_drawdown, 2),
            average_win=round(average_win, 4),
            average_loss=round(average_loss, 4),
            expectancy=round(expectancy, 4),
            best_setup=best_setup,
            worst_setup=worst_setup,
            session_performance={session_name: round(sum(pnls), 4)},
            coin_performance={symbol: round(sum(pnls), 4)},
            equity_curve=[round(value, 4) for value in simulator.equity_curve],
        )
