from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


@dataclass(slots=True)
class PaperPosition:
    id: str
    symbol: str
    side: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit_levels: list[float]
    remaining_quantity: float
    opened_at: datetime
    realized_pnl: float = 0.0
    fees: float = 0.0
    status: str = "open"


@dataclass(slots=True)
class PaperFill:
    trade_id: str
    symbol: str
    side: str
    price: float
    quantity: float
    pnl: float
    fee: float
    reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PaperTradingSimulator:
    """In-memory paper engine with fees, slippage, partial exits, stops, and drawdown tracking."""

    def __init__(self, starting_equity: float = 10_000.0, fee_bps: float = 6.0, slippage_bps: float = 4.0) -> None:
        self.starting_equity = starting_equity
        self.equity = starting_equity
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.positions: dict[str, PaperPosition] = {}
        self.fills: list[PaperFill] = []
        self.equity_curve: list[float] = [starting_equity]
        self.max_drawdown = 0.0

    @property
    def daily_pnl_pct(self) -> float:
        return (self.equity - self.starting_equity) / self.starting_equity * 100

    def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: float,
        stop_loss: float,
        take_profit_levels: list[float],
    ) -> PaperPosition:
        trade_id = str(uuid4())
        fill_price = self._apply_slippage(entry_price, side, entering=True)
        fee = fill_price * quantity * self.fee_bps / 10_000
        self.equity -= fee
        position = PaperPosition(
            id=trade_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit_levels=take_profit_levels,
            remaining_quantity=quantity,
            opened_at=datetime.now(timezone.utc),
            fees=fee,
        )
        self.positions[trade_id] = position
        self._record_equity()
        return position

    def update_price(self, symbol: str, price: float) -> list[PaperFill]:
        generated: list[PaperFill] = []
        for position in list(self.positions.values()):
            if position.symbol != symbol or position.status != "open":
                continue
            hit_stop = price <= position.stop_loss if position.side == "long" else price >= position.stop_loss
            if hit_stop:
                generated.append(self.close_position(position.id, price, "stop_loss"))
                continue
            for index, target in enumerate(position.take_profit_levels[:2]):
                hit_target = price >= target if position.side == "long" else price <= target
                marker = f"tp{index + 1}"
                already_hit = any(fill.trade_id == position.id and fill.reason == marker for fill in self.fills)
                if hit_target and not already_hit and position.remaining_quantity > 0:
                    fraction = 0.4 if index == 0 else 0.3
                    generated.append(self.partial_close(position.id, price, fraction, marker))
            final_target = position.take_profit_levels[-1] if position.take_profit_levels else None
            if final_target is not None:
                final_hit = price >= final_target if position.side == "long" else price <= final_target
                if final_hit and position.remaining_quantity > 0:
                    generated.append(self.close_position(position.id, price, "trailing_runner_target"))
        return generated

    def partial_close(self, trade_id: str, price: float, fraction: float, reason: str) -> PaperFill:
        position = self.positions[trade_id]
        quantity = min(position.remaining_quantity, position.quantity * fraction)
        fill = self._exit_fill(position, price, quantity, reason)
        position.remaining_quantity -= quantity
        position.realized_pnl += fill.pnl
        position.fees += fill.fee
        if position.remaining_quantity <= 1e-12:
            position.status = "closed"
            self.positions.pop(trade_id, None)
        self.fills.append(fill)
        self._record_equity()
        return fill

    def close_position(self, trade_id: str, price: float, reason: str = "manual") -> PaperFill:
        position = self.positions[trade_id]
        fill = self._exit_fill(position, price, position.remaining_quantity, reason)
        position.remaining_quantity = 0.0
        position.realized_pnl += fill.pnl
        position.fees += fill.fee
        position.status = "closed"
        self.positions.pop(trade_id, None)
        self.fills.append(fill)
        self._record_equity()
        return fill

    def close_all(self, prices: dict[str, float], reason: str = "emergency_close") -> list[PaperFill]:
        fills: list[PaperFill] = []
        for trade_id, position in list(self.positions.items()):
            fills.append(self.close_position(trade_id, prices.get(position.symbol, position.entry_price), reason))
        return fills

    def _exit_fill(self, position: PaperPosition, price: float, quantity: float, reason: str) -> PaperFill:
        exit_side = "sell" if position.side == "long" else "buy"
        fill_price = self._apply_slippage(price, exit_side, entering=False)
        gross = (fill_price - position.entry_price) * quantity
        if position.side == "short":
            gross *= -1
        fee = fill_price * quantity * self.fee_bps / 10_000
        pnl = gross - fee
        self.equity += pnl
        return PaperFill(position.id, position.symbol, position.side, fill_price, quantity, pnl, fee, reason)

    def _apply_slippage(self, price: float, side: str, entering: bool) -> float:
        adjustment = price * self.slippage_bps / 10_000
        if entering:
            return price + adjustment if side in {"buy", "long"} else price - adjustment
        return price - adjustment if side == "sell" else price + adjustment

    def _record_equity(self) -> None:
        self.equity_curve.append(self.equity)
        peak = max(self.equity_curve)
        drawdown = (peak - self.equity) / peak * 100 if peak else 0.0
        self.max_drawdown = max(self.max_drawdown, drawdown)
